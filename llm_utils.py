#!/usr/bin/env python3
from openai import OpenAI
import os
import json
from dotenv import load_dotenv
import httpx

load_dotenv()


def tokens_to_chars(tokens: int) -> int:
    """Convert token count to approximate character count.
    
    Uses CHAR_TOKEN_RATIO environment variable (default: 3.6).
    """
    ratio = float(os.getenv('CHAR_TOKEN_RATIO', '3.6'))
    return int(tokens * ratio)


def _get_ollama_context_size(base_url: str, model_id: str) -> int:
    """Get context size from Ollama /api/show endpoint."""
    try:
        if not base_url:
            raise ValueError("Base URL is required for Ollama fallback")
        
        ollama_base = base_url.rstrip('/')
        if ollama_base.endswith('/v1'):
            ollama_base = ollama_base[:-3]
        
        show_url = f"{ollama_base}/api/show"
        
        with httpx.Client(timeout=10.0) as http_client:
            response = http_client.post(
                show_url,
                json={"name": model_id},
                headers={"Content-Type": "application/json"}
            )
            response.raise_for_status()
            data = response.json()
            
            if 'modelfile' in data:
                modelfile = data['modelfile']
                for line in modelfile.split('\n'):
                    line = line.strip()
                    if line.startswith('PARAMETER num_ctx') or line.startswith('num_ctx'):
                        parts = line.split()
                        for i, part in enumerate(parts):
                            if part == 'num_ctx' and i + 1 < len(parts):
                                try:
                                    return int(parts[i + 1])
                                except (ValueError, IndexError):
                                    pass
            
            if 'parameters' in data and 'num_ctx' in data['parameters']:
                return int(data['parameters']['num_ctx'])
            
            if 'details' in data:
                details = data['details']
                if 'num_ctx' in details:
                    return int(details['num_ctx'])
                if 'context_length' in details:
                    return int(details['context_length'])
            
            if 'model_info' in data:
                model_info = data['model_info']
                for key, value in model_info.items():
                    if key.endswith('.context_length') and isinstance(value, (int, str)):
                        try:
                            return int(value)
                        except (ValueError, TypeError):
                            pass
                if 'context_length' in model_info:
                    try:
                        return int(model_info['context_length'])
                    except (ValueError, TypeError):
                        pass
            
            print(f"DEBUG: Raw Ollama /api/show response for model {model_id}:")
            print(json.dumps(data, indent=2))
            raise ValueError(f"Ollama model {model_id} found but has no context size information in /api/show response")
    except httpx.HTTPError as e:
        raise ValueError(f"Could not fetch model info from Ollama API: {e}") from e
    except (KeyError, ValueError, TypeError) as e:
        raise ValueError(f"Could not parse context size from Ollama API response: {e}") from e


def get_model_context_size(client: OpenAI, model_id: str, model_type: str = None) -> int:
    if model_type == 'llm':
        env_limit = os.getenv('LLM_CONTEXT_LIMIT')
        if env_limit:
            try:
                return int(env_limit)
            except (ValueError, TypeError):
                pass
    elif model_type == 'emb':
        env_limit = os.getenv('EMB_CONTEXT_LIMIT')
        if env_limit:
            try:
                return int(env_limit)
            except (ValueError, TypeError):
                pass
    
    base_url_str = None
    if hasattr(client, 'base_url'):
        base_url_str = str(client.base_url)
    elif hasattr(client, '_client') and hasattr(client._client, 'base_url'):
        base_url_str = str(client._client.base_url)
    
    is_ollama = base_url_str and ('ollama' in base_url_str.lower() or ':11434' in base_url_str)
    
    try:
        models = client.models.list()
        for model in models.data:
            if model.id == model_id:
                if hasattr(model, 'context_length') and model.context_length:
                    return model.context_length
                if hasattr(model, 'max_input_tokens') and model.max_input_tokens:
                    return model.max_input_tokens
                if hasattr(model, 'max_model_len') and model.max_model_len:
                    return model.max_model_len
                
                if is_ollama:
                    try:
                        return _get_ollama_context_size(base_url_str, model_id)
                    except ValueError as ollama_error:
                        raise ValueError(f"Model {model_id} found but has no context size information (checked: context_length, max_input_tokens, max_model_len). Ollama fallback also failed: {ollama_error}") from ollama_error
                
                raise ValueError(f"Model {model_id} found but has no context size information (checked: context_length, max_input_tokens, max_model_len)")
        raise ValueError(f"Model {model_id} not found in API")
    except ValueError as e:
        if is_ollama and "not found" in str(e).lower():
            try:
                return _get_ollama_context_size(base_url_str, model_id)
            except ValueError as ollama_error:
                raise ValueError(f"{e}. Ollama fallback also failed: {ollama_error}") from ollama_error
        raise
    except Exception as e:
        if is_ollama:
            try:
                return _get_ollama_context_size(base_url_str, model_id)
            except ValueError as ollama_error:
                raise ValueError(f"Could not fetch model info from API: {e}. Ollama fallback also failed: {ollama_error}") from ollama_error
        
        raise ValueError(f"Could not fetch model info from API: {e}") from e

