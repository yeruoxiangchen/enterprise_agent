"""Bounded, redacted backend diagnostics; never log headers or request bodies."""
import json
import re
import time


class ModelServiceError(RuntimeError):
    def __init__(self,status,diagnostic):
        self.status,self.diagnostic=status,diagnostic
        super().__init__(f"模型服务返回 HTTP {status}（{diagnostic['category']}），详情已写入本任务 logs/model_errors.jsonl")


def record(settings,response,attempt):
    try:
        body=response.json();error=body.get('error',body) if isinstance(body,dict) else {}
        error=error if isinstance(error,dict) else {'message':str(error)}
    except ValueError:
        error={'message':'non_json_error_response'}
    message=str(error.get('message',''))
    if settings.llm_key:message=message.replace(settings.llm_key,'[REDACTED]')
    message=re.sub(r'(?i)bearer\s+\S+','Bearer [REDACTED]',message)
    message=re.sub(r'\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,})','[REDACTED]',message)
    category='backend_error'
    if re.search(r'json_invalid|invalid json|EOF while parsing|unterminated',message,re.I):category='tool_output_invalid_json'
    elif re.search(r'context|maximum.*tokens|max_tokens',message,re.I):category='context_or_token_budget'
    elif re.search(r'schema|grammar|tool_choice|tool.*parser',message,re.I):category='tool_configuration'
    d={'at':time.time(),'status':response.status_code,'category':category,'attempt':attempt,
       'backend_type':str(error.get('type',''))[:80], 'message':message[:700],
       'configured_output_tokens':settings.output_tokens,'request_headers_recorded':False}
    path=settings.root/'logs'/'model_errors.jsonl';path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a') as f:f.write(json.dumps(d,ensure_ascii=False)+'\n')
    return ModelServiceError(response.status_code,d)
