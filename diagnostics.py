"""Bounded, redacted diagnostics before Hermes consumes provider failures.

Never log requests, environment dictionaries, account paths or assistant output.
Logging failure must not alter the provider exception or retry behavior.
"""
import json
import logging
import os
import re
import traceback

LOG = logging.getLogger('claude_directsdk.diagnostics')


def scrub(value, secrets=()):
    text = str(value)
    for secret in sorted({str(s) for s in secrets if s and len(str(s)) >= 4}, key=len, reverse=True):
        text = text.replace(secret, '[REDACTED]')
    text = re.sub(r'(?i)\b(?:Bearer|Basic)\s+[^\s,;\"\']+', '[REDACTED AUTH]', text)
    text = re.sub(r'\bsk-[A-Za-z0-9_-]+', '[REDACTED KEY]', text)
    text = re.sub(r'(?i)([\"\']?(?:authorization|cookie|api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)[\"\']?\s*[:=]\s*)(?:\"[^\"]*\"|\'[^\']*\'|[^\s,;}]+)', r'\1[REDACTED]', text)
    text = re.sub(r'https?://[^\s<>\"\']+', '[REDACTED URL]', text)
    return text[:4000]


def record(kind, model, *, error=None, api_key=None, env=None, admission=None,
           process=None, stderr=(), detail=None):
    try:
        secrets = [api_key]
        for source in (os.environ, env or {}):
            secrets.extend(v for k, v in source.items() if any(s in k.upper() for s in ('TOKEN', 'KEY', 'PASSWORD', 'SECRET', 'COOKIE', 'CREDENTIAL', 'CONFIG_DIR')))
        fields = {'event': kind, 'model': scrub(model, secrets)}
        if error is not None:
            fields.update(exception=type(error).__name__, message=scrub(error, secrets),
                          status=getattr(error, 'status_code', None),
                          stack=[f'{os.path.basename(f.filename)}:{f.lineno}:{f.name}'
                                 for f in traceback.extract_tb(error.__traceback__)[-12:]])
        if admission is not None:
            for key in ('status', 'request_id', 'used', 'denied', 'failure'):
                value = getattr(admission, key, None)
                fields['upstream_' + key] = scrub(value, secrets) if isinstance(value, str) else value
            capture = getattr(admission, 'capture', None)
            fields['capture_complete'] = getattr(capture, 'complete', None)
        if process is not None:
            fields['native_exit_code'] = process.poll()
        if stderr:
            fields['native_stderr'] = scrub(''.join(list(stderr)[-40:]), secrets)
        if detail:
            fields['detail'] = scrub(detail, secrets)
        LOG.warning('DirectSDK diagnostic %s', json.dumps(fields, ensure_ascii=True))
    except Exception:
        # Preserve the original error even when a log handler or formatter fails.
        pass
