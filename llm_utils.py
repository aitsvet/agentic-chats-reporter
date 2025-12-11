#!/usr/bin/env python3
from openai import OpenAI
import os
import json
import sys
import time
import re
from typing import Dict, Any, Optional, Callable, TypeVar, Tuple
from functools import wraps
from dotenv import load_dotenv
import httpx

load_dotenv()

T = TypeVar('T')


def tokens_to_chars(tokens: int) -> int:
    """Convert token count to approximate character count.
    
    Uses CHAR_TOKEN_RATIO environment variable (default: 3.6).
    """
    ratio = float(os.getenv('CHAR_TOKEN_RATIO', '3.6'))
    return int(tokens * ratio)


def chars_to_tokens(chars: int) -> int:
    """Convert character count to approximate token count.
    
    Uses CHAR_TOKEN_RATIO environment variable (default: 3.6).
    """
    ratio = float(os.getenv('CHAR_TOKEN_RATIO', '3.6'))
    return int(chars / ratio)


def parse_llm_params(env_var_name: str, default_params: Dict[str, Any]) -> Dict[str, Any]:
    """Parse LLM API parameters from JSON environment variable with defaults.
    
    Args:
        env_var_name: Name of environment variable containing JSON parameters
        default_params: Dictionary of default parameter values
    
    Returns:
        Dictionary of parameters with user values merged over defaults
    """
    env_value = os.getenv(env_var_name)
    if not env_value:
        return default_params.copy()
    
    try:
        user_params = json.loads(env_value)
        result = default_params.copy()
        result.update(user_params)
        return result
    except json.JSONDecodeError as e:
        print(f"Warning: Invalid JSON in {env_var_name}: {e}. Using defaults.", file=sys.stderr)
        return default_params.copy()


def get_output_reserve_tokens(full_context_size: int, max_tokens: int = None) -> int:
    """Calculate output reserve tokens from max_tokens or default.
    
    Args:
        full_context_size: Full model context size in tokens
        max_tokens: Maximum output tokens to reserve (if None, uses 25% of context as default)
    
    Returns:
        Number of tokens to reserve for output
    """
    if max_tokens is not None and max_tokens > 0:
        return max_tokens
    else:
        return int(full_context_size * 0.25)


def get_effective_context_size(full_context_size: int, max_tokens: int = None) -> int:
    """Calculate effective context size available for input after reserving space for output.
    
    Args:
        full_context_size: Full model context size in tokens
        max_tokens: Maximum output tokens to reserve (if None, uses 25% of context as default)
    
    Returns:
        Effective context size in tokens available for input
    """
    output_reserve = get_output_reserve_tokens(full_context_size, max_tokens)
    effective_size = full_context_size - output_reserve
    return max(effective_size, int(full_context_size * 0.5))


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


def _try_ollama_fallback(base_url_str: str, model_id: str, primary_error: Exception) -> int:
    """Try Ollama fallback for context size fetching.
    
    Args:
        base_url_str: Base URL string
        model_id: Model identifier
        primary_error: The primary error that triggered this fallback
    
    Returns:
        Context size from Ollama API
    
    Raises:
        ValueError: If Ollama fallback also fails
    """
    try:
        return _get_ollama_context_size(base_url_str, model_id)
    except ValueError as ollama_error:
        error_msg = str(primary_error)
        if "not found" in error_msg.lower():
            raise ValueError(f"{error_msg}. Ollama fallback also failed: {ollama_error}") from ollama_error
        else:
            raise ValueError(f"Could not fetch model info from API: {primary_error}. Ollama fallback also failed: {ollama_error}") from ollama_error


def get_model_context_size(client: OpenAI, model_id: str, model_type: str = None) -> int:
    if model_type == 'emb':
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
                    raise ValueError(f"Model {model_id} found but has no context size information (checked: context_length, max_input_tokens, max_model_len)")
                
                raise ValueError(f"Model {model_id} found but has no context size information (checked: context_length, max_input_tokens, max_model_len)")
        raise ValueError(f"Model {model_id} not found in API")
    except ValueError as e:
        if is_ollama:
            return _try_ollama_fallback(base_url_str, model_id, e)
        raise
    except Exception as e:
        if is_ollama:
            return _try_ollama_fallback(base_url_str, model_id, e)
        raise ValueError(f"Could not fetch model info from API: {e}") from e


DEFAULT_SUMMARY_PARAMS = {
    'stop': None,
    'temperature': 0.1,
    'frequency_penalty': None,
    'presence_penalty': None,
    'top_p': None,
    'max_tokens': None,
    'seed': None,
    'n': 1,
    'logprobs': False,
    'top_logprobs': None,
    'logit_bias': None,
    'user': None,
    'response_format': None
}

DEFAULT_SPEC_PARAMS = {
    'stop': None,
    'temperature': 0.3,
    'frequency_penalty': None,
    'presence_penalty': None,
    'top_p': None,
    'max_tokens': None,
    'seed': None,
    'n': 1,
    'logprobs': False,
    'top_logprobs': None,
    'logit_bias': None,
    'user': None,
    'response_format': None
}


def create_openai_client(base_url: str, api_key: Optional[str] = None, 
                         env_key_name: str = None, default_key: str = 'not-needed',
                         timeout: Optional[float] = None) -> OpenAI:
    """Create OpenAI client with proper API key resolution.
    
    Args:
        base_url: Base URL for the API
        api_key: Explicit API key (takes precedence)
        env_key_name: Environment variable name for API key
        default_key: Default API key if not found in env (default: 'not-needed')
        timeout: Optional timeout in seconds for HTTP client
    
    Returns:
        Configured OpenAI client instance
    """
    if api_key is None and env_key_name:
        api_key = os.getenv(env_key_name, default_key)
    
    kwargs = {'base_url': base_url, 'api_key': api_key}
    if timeout:
        http_client = httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0))
        kwargs['http_client'] = http_client
    
    return OpenAI(**kwargs)


def retry_with_backoff(max_retries: int = None, base_delay: float = None,
                      retry_env_prefix: str = 'LLM', 
                      debug: bool = None) -> Callable:
    """Decorator for retrying API calls with exponential backoff.
    
    Args:
        max_retries: Maximum retry attempts (default: from {retry_env_prefix}_MAX_RETRIES env var)
        base_delay: Base delay in seconds (default: from {retry_env_prefix}_RETRY_DELAY env var)
        retry_env_prefix: Prefix for environment variables (default: 'LLM')
        debug: Enable debug output (default: from DEBUG_LLM env var)
    
    Returns:
        Decorator function
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> T:
            nonlocal max_retries, base_delay, debug
            
            if max_retries is None:
                max_retries = int(os.getenv(f'{retry_env_prefix}_MAX_RETRIES', '3'))
            if base_delay is None:
                base_delay = float(os.getenv(f'{retry_env_prefix}_RETRY_DELAY', '1.0'))
            if debug is None:
                debug = os.getenv('DEBUG_LLM') == '1'
            
            last_error = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_error = e
                    if attempt < max_retries - 1:
                        delay = base_delay * (2 ** attempt)
                        if debug:
                            print(f"    API error (attempt {attempt + 1}/{max_retries}): {e}, retrying in {delay:.1f}s...")
                            sys.stdout.flush()
                        time.sleep(delay)
                    else:
                        raise RuntimeError(f"Failed after {max_retries} attempts: {e}") from e
            
            raise RuntimeError(f"Failed after {max_retries} attempts: {last_error}") from last_error
        return wrapper
    return decorator


def get_llm_context_limit_and_max_tokens(client: OpenAI, model_id: str, 
                                         params: Dict[str, Any] = None) -> Tuple[int, Optional[int]]:
    """Get LLM context limit and max_tokens parameter to send.
    
    Rules:
    - If max_tokens is set in params and > 0: use it as context limit and send it as parameter
    - If max_tokens is set to <= 0: retrieve from API, use as context limit and send as parameter
    - If LLM_CONTEXT_LIMIT is set: use it as context limit but don't send as parameter (unless max_tokens is set)
    - If LLM_CONTEXT_LIMIT is not set: retrieve from API, use as context limit but don't send as parameter
    
    Args:
        client: OpenAI client instance
        model_id: Model identifier
        params: Optional dict with 'max_tokens' key
    
    Returns:
        Tuple of (context_limit_tokens, max_tokens_to_send)
        max_tokens_to_send is None if it should not be sent to API
    """
    params_max_tokens = (params or {}).get('max_tokens')
    llm_context_limit = os.getenv('LLM_CONTEXT_LIMIT')
    
    if params_max_tokens is not None:
        if params_max_tokens > 0:
            return (params_max_tokens, params_max_tokens)
        else:
            api_context_size = get_model_context_size(client, model_id, model_type='llm')
            return (api_context_size, api_context_size)
    
    if llm_context_limit:
        try:
            context_limit = int(llm_context_limit)
            return (context_limit, None)
        except (ValueError, TypeError):
            pass
    
    api_context_size = get_model_context_size(client, model_id, model_type='llm')
    return (api_context_size, None)


def get_effective_llm_context_size(client: OpenAI, model_id: str, 
                                  params: Dict[str, Any] = None,
                                  safety_factor: float = 0.8) -> int:
    """Get effective LLM context size with safety factor applied.

    
    If max_tokens is set in params, it is used as the overall content size limit
    and safety_factor is applied to it. Otherwise, uses API-reported context size
    with output reserve calculation, then applies safety_factor.
    
    Args:
        client: OpenAI client instance (only used if max_tokens not set)
        model_id: Model identifier (only used if max_tokens not set)
        params: Optional dict with 'max_tokens' key
        safety_factor: Multiplier for context size (default: 0.8)
    
    Returns:
        Effective context size in tokens after applying safety factor
    """
    max_tokens = (params or {}).get('max_tokens')
    if max_tokens is not None and max_tokens > 0:
        return int(max_tokens * safety_factor)
    
    full_context_size = get_model_context_size(client, model_id, model_type='llm')
    effective_size = get_effective_context_size(full_context_size, max_tokens=None)
    return int(effective_size * safety_factor)


def load_api_config(required_vars: list = None) -> Dict[str, str]:
    """Load API configuration from environment variables.
    
    Args:
        required_vars: List of required config keys (e.g., ['emb_url', 'emb_model'])
    
    Returns:
        Dictionary with API configuration values
    
    Raises:
        ValueError: If required variables are not set
    """
    config = {
        'emb_url': os.getenv('EMB_URL'),
        'emb_model': os.getenv('EMB_MODEL'),
        'emb_api_key': os.getenv('EMB_API_KEY'),
        'llm_url': os.getenv('LLM_URL'),
        'llm_model': os.getenv('LLM_MODEL'),
        'llm_api_key': os.getenv('LLM_API_KEY'),
    }
    
    if required_vars:
        missing = [var for var in required_vars if not config.get(var)]
        if missing:
            raise ValueError(f"Required environment variables not set: {', '.join(missing)}")
    
    return config


def get_llm_params(env_var_name: str, default_params: Dict[str, Any] = None) -> Dict[str, Any]:
    """Get LLM parameters from environment variable with defaults.
    
    Args:
        env_var_name: Environment variable name containing JSON parameters
        default_params: Default parameter dictionary (uses DEFAULT_SUMMARY_PARAMS if None)
    
    Returns:
        Dictionary of LLM parameters
    """
    if default_params is None:
        default_params = DEFAULT_SUMMARY_PARAMS
    return parse_llm_params(env_var_name, default_params)


def clean_llm_response(response_text: str) -> str:
    """Clean LLM response by removing redacted reasoning tags.
    
    Args:
        response_text: Raw LLM response text
    
    Returns:
        Cleaned response text with redacted reasoning tags removed
    """
    cleaned = re.sub(r'<think>.*?</think>', '', response_text, flags=re.DOTALL)
    return cleaned.strip()

