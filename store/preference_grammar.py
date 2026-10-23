"""Deliberately narrow user-only preference grammar; no model or inferred profile.

An entire user message must match. Quoted, historical, third-party and compound
statements fail closed. No arbitrary text is converted into preference data.
"""
import re

KEYS = {'Hindi': ('preferred_response_language', 'hi'),
        'English': ('preferred_response_language', 'en'),
        'concise': ('preferred_response_format', 'concise'),
        'detailed': ('preferred_response_format', 'detailed')}
GRAMMAR = 'Always reply in Hindi/English; Always be concise/detailed; Reply in Hindi/English for this conversation/response; Be concise/detailed for this conversation/response.'

def extract(text, conversation_id=None):
    if not isinstance(text, str) or len(text)>1000:
        return []
    match = re.fullmatch(r'(Always reply in (Hindi|English)|Always be (concise|detailed)|Reply in (Hindi|English) for this (conversation|response)|Be (concise|detailed) for this (conversation|response))\.?',text.strip(),re.IGNORECASE)
    if not match:
        return []
    words=text.strip().rstrip('.').lower()
    item=next((value for key,value in KEYS.items() if re.search(r'\b'+key.lower()+r'\b',words)),None)
    scope='global' if words.startswith('always ') else 'response' if words.endswith('response') else 'conversation'
    if scope!='global' and not conversation_id:
        return []
    return [dict(key=item[0],value=item[1],operation='SET',scope_key='global' if scope=='global' else scope+':'+str(conversation_id))]
