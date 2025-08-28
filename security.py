# security.py
"""
Cybersecurity utilities for the AI itinerary backend.
Protects against prompt injection, XSS, and other attacks.
"""
from __future__ import annotations

import re
import html
import logging
from typing import List, Optional, Set
from functools import wraps
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
import time
from collections import defaultdict, deque

log = logging.getLogger("security")

# Suspicious patterns that might indicate prompt injection
PROMPT_INJECTION_PATTERNS = [
    # Direct instruction attempts
    r'\b(ignore|forget|disregard)\s+(previous|above|all|these|your)\s+(instructions?|prompts?|rules?)\b',
    r'\bignore\s+.*\binstructions?\b',
    r'\b(act|behave|pretend|roleplay)\s+as\s+(a|an)?\s*\w+',
    r'\b(you\s+are|now\s+you\s+are)\s+(a|an|now)?\s*\w+',
    r'\bnow\s+(respond|answer|say|tell|write|generate)\b',
    
    # System prompt manipulation
    r'\bsystem\s*:?\s*',
    r'\b(assistant|ai|chatbot|gpt|claude)\s*:?\s*',
    r'\buser\s*:?\s*',
    r'<\s*/?system\s*>',
    r'<\s*/?assistant\s*>',
    r'<\s*/?user\s*>',
    
    # Jailbreak attempts
    r'\b(jailbreak|bypass|override|hack|exploit)\b',
    r'\bfor\s+educational\s+purposes?\b',
    r'\bhypothetically?\b',
    r'\bin\s+the\s+context\s+of\b',
    r'\bimagine\s+if\b',
    r'\blet\'?s\s+say\b',
    
    # Code injection attempts
    r'```\s*(python|javascript|bash|sh|cmd|powershell|sql)',
    r'\beval\s*\(',
    r'\bexec\s*\(',
    r'\b__import__\s*\(',
    r'\bos\.(system|popen|exec)',
    
    # Prompt continuation tricks
    r'\.\.\.',
    r'continued?:',
    r'part\s+\d+\s*:',
    r'step\s+\d+\s*:',
    
    # Social engineering
    r'\b(emergency|urgent|critical|important|please|help)\b.*\b(override|ignore|bypass)\b',
    r'\bi\s+am\s+(your\s+)?(creator|developer|admin|owner)\b',
    r'\bmy\s+(grandmother|dying)\b',
    
    # Multi-language injection attempts
    r'\\u[0-9a-fA-F]{4}',  # Unicode escape sequences
    r'&#\d+;',  # HTML entities
    r'%[0-9a-fA-F]{2}',  # URL encoding
]

# Compile patterns for efficiency
COMPILED_PATTERNS = [re.compile(pattern, re.IGNORECASE | re.MULTILINE) for pattern in PROMPT_INJECTION_PATTERNS]

# Rate limiting storage
rate_limit_storage = defaultdict(lambda: deque())

def sanitize_input(text: str, max_length: int = 500) -> str:
    """
    Sanitize user input to prevent XSS and injection attacks.
    
    Args:
        text: Input text to sanitize
        max_length: Maximum allowed length
        
    Returns:
        Sanitized text
        
    Raises:
        HTTPException: If input is too long or contains suspicious content
    """
    if not isinstance(text, str):
        raise HTTPException(status_code=400, detail="Input must be a string")
    
    # Check length
    if len(text) > max_length:
        log.warning("Input length exceeded", extra={"length": len(text), "max_length": max_length})
        raise HTTPException(status_code=400, detail=f"Input too long. Maximum {max_length} characters allowed.")
    
    # HTML escape to prevent XSS
    sanitized = html.escape(text.strip())
    
    # Remove null bytes and control characters
    sanitized = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', sanitized)
    
    # Normalize whitespace
    sanitized = re.sub(r'\s+', ' ', sanitized).strip()
    
    return sanitized

def detect_prompt_injection(text: str) -> tuple[bool, List[str]]:
    """
    Detect potential prompt injection attempts.
    
    Args:
        text: Input text to analyze
        
    Returns:
        Tuple of (is_suspicious, list_of_matched_patterns)
    """
    suspicious_patterns = []
    text_lower = text.lower()
    
    for i, pattern in enumerate(COMPILED_PATTERNS):
        if pattern.search(text):
            suspicious_patterns.append(PROMPT_INJECTION_PATTERNS[i])
    
    # Additional heuristics
    # Check for excessive special characters
    special_char_ratio = len(re.findall(r'[^\w\s]', text)) / max(len(text), 1)
    if special_char_ratio > 0.3:
        suspicious_patterns.append("excessive_special_characters")
    
    # Check for repeated suspicious keywords
    suspicious_keywords = ['system', 'ignore', 'override', 'jailbreak', 'bypass', 'hack', 'prompt', 'instruction']
    keyword_count = sum(text_lower.count(keyword) for keyword in suspicious_keywords)
    if keyword_count > 3:
        suspicious_patterns.append("excessive_suspicious_keywords")
    
    return len(suspicious_patterns) > 0, suspicious_patterns

def validate_destination(destination: str) -> str:
    """
    Validate and sanitize destination input.
    
    Args:
        destination: Destination string
        
    Returns:
        Sanitized destination
        
    Raises:
        HTTPException: If destination is invalid
    """
    if not destination:
        raise HTTPException(status_code=400, detail="Destination cannot be empty")
    
    # Sanitize
    clean_destination = sanitize_input(destination, max_length=100)
    
    # Check for prompt injection
    is_suspicious, patterns = detect_prompt_injection(clean_destination)
    if is_suspicious:
        log.warning("Suspicious destination detected", extra={
            "destination": destination,
            "patterns": patterns
        })
        raise HTTPException(
            status_code=400, 
            detail="Invalid destination. Please provide a valid city or location name."
        )
    
    # Basic validation - should contain at least some letters
    if not re.search(r'[a-zA-Z]', clean_destination):
        raise HTTPException(status_code=400, detail="Destination must contain letters")
    
    # Check for reasonable patterns (city names, not code or instructions)
    if len(clean_destination.split()) > 10:  # Too many words might be suspicious
        raise HTTPException(status_code=400, detail="Destination name too complex")
    
    return clean_destination

def validate_interests(interests: List[str]) -> List[str]:
    """
    Validate and sanitize interests list.
    
    Args:
        interests: List of interests
        
    Returns:
        Sanitized interests list
        
    Raises:
        HTTPException: If interests are invalid
    """
    if not interests:
        return []
    
    if len(interests) > 20:
        raise HTTPException(status_code=400, detail="Too many interests. Maximum 20 allowed.")
    
    sanitized_interests = []
    
    for interest in interests:
        if not isinstance(interest, str):
            continue
            
        clean_interest = sanitize_input(interest, max_length=50)
        
        # Check for prompt injection
        is_suspicious, patterns = detect_prompt_injection(clean_interest)
        if is_suspicious:
            log.warning("Suspicious interest detected", extra={
                "interest": interest,
                "patterns": patterns
            })
            continue  # Skip suspicious interests rather than failing completely
        
        if clean_interest:
            sanitized_interests.append(clean_interest)
    
    return sanitized_interests

def rate_limit(max_requests: int = 10, window_seconds: int = 60):
    """
    Rate limiting decorator.
    
    Args:
        max_requests: Maximum requests allowed in the time window
        window_seconds: Time window in seconds
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(request: Request, *args, **kwargs):
            # Get client IP (considering proxy headers)
            client_ip = (
                request.headers.get("x-forwarded-for", "").split(",")[0].strip() or
                request.headers.get("x-real-ip") or
                request.client.host if request.client else "unknown"
            )
            
            current_time = time.time()
            client_requests = rate_limit_storage[client_ip]
            
            # Remove old requests outside the window
            while client_requests and client_requests[0] < current_time - window_seconds:
                client_requests.popleft()
            
            # Check rate limit
            if len(client_requests) >= max_requests:
                log.warning("Rate limit exceeded", extra={
                    "client_ip": client_ip,
                    "requests_count": len(client_requests),
                    "window_seconds": window_seconds
                })
                raise HTTPException(
                    status_code=429, 
                    detail=f"Rate limit exceeded. Maximum {max_requests} requests per {window_seconds} seconds."
                )
            
            # Add current request
            client_requests.append(current_time)
            
            return await func(request, *args, **kwargs)
        return wrapper
    return decorator

def security_headers_middleware():
    """
    Add security headers to responses.
    """
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        
        # Security headers
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Content-Security-Policy"] = "default-src 'self'"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        
        return response
    
    return add_security_headers

class SecurityValidator:
    """
    Main security validation class for request processing.
    """
    
    @staticmethod
    def validate_request_size(request_size: int, max_size: int = 1024 * 10):  # 10KB default
        """Validate request size to prevent DOS attacks."""
        if request_size > max_size:
            log.warning("Request size too large", extra={"size": request_size, "max_size": max_size})
            raise HTTPException(
                status_code=413, 
                detail=f"Request too large. Maximum {max_size} bytes allowed."
            )
    
    @staticmethod
    def log_security_event(event_type: str, details: dict):
        """Log security events for monitoring."""
        log.warning(f"Security event: {event_type}", extra={
            "event_type": event_type,
            **details
        })

# Additional pattern for detecting base64 encoded payloads
def detect_encoded_injection(text: str) -> bool:
    """Detect potentially encoded injection attempts."""
    import base64
    
    # Look for base64-like strings
    b64_pattern = re.compile(r'[A-Za-z0-9+/]{20,}={0,2}')
    matches = b64_pattern.findall(text)
    
    for match in matches:
        try:
            decoded = base64.b64decode(match + '==').decode('utf-8', errors='ignore')
            is_suspicious, _ = detect_prompt_injection(decoded)
            if is_suspicious:
                return True
        except Exception:
            pass
    
    return False