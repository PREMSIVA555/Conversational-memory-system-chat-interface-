"""Bind narrow explicit applicability before a model-selected policy read (INV09).

This is not general intent extraction: ambiguous or indirect filters clarify.
"""
import re
from store.banking_policies import instant

LOCALE = re.compile(r'(?<![\w-])[a-z]{2,3}(?:-[a-z]{4})?-(?:[a-z]{2}|\d{3})(?![\w-])',re.I)
DATE = re.compile(r'\b\d{4}-\d{2}-\d{2}(?:[Tt][\d:.]+(?:[Zz]|[+-]\d{2}:\d{2})?)?')
SCOPE = re.compile(r'\bscope\s*[:=]?\s*([a-zA-Z0-9_.:-]{1,80})\b',re.I)


def bind_policy_request(request, proposed, *, language='en'):
    """Return bound arguments or None; a model cannot erase a user constraint."""
    if proposed['product'].replace('-',' ').lower() not in request.replace('-',' ').lower():
        return None
    locales=LOCALE.findall(request)
    dates=DATE.findall(request)
    scopes=SCOPE.findall(request)+re.findall(r'\b(?:retail|business|corporate|wholesale)\b',request,re.I)
    # ponytail: conservative grammar; indirect/quoted/negative filters clarify,
    # rather than claiming general conversational provenance understanding.
    if re.search(r'\b(?:not|except|excluding|instead|previously|earlier|said|quoted)\b|["`“”‘’]|\x27[^\x27]+\x27',request,re.I):
        return None
    if len(set(v.lower() for v in locales))>1 or len(set(dates))>1 or len(set(v.lower() for v in scopes))>1:
        return None
    if re.search(r'\bscope\b',request,re.I) and not scopes:
        return None
    if re.search(r'\blocale\b',request,re.I) and not locales:
        return None
    if not dates and re.search(r'\b(?:at|as of|effective|yesterday|tomorrow|last year|last month|previous year)\b|\d+[/-]\d+[/-]\d+',request,re.I):
        return None
    if locales and re.search(r'\b(?:respond|reply|answer|translate|language)\b',request,re.I):
        return None  # A language-tag mention is not necessarily applicability.
    bound=dict(proposed)
    for field,values in [('locale',locales),('scope',scopes),('at',dates)]:
        explicit=values[0] if values else None
        chosen=proposed.get(field)
        if explicit is not None:
            if field=='at':
                try:
                    if chosen is not None and instant(chosen)!=instant(explicit):return None
                    instant(explicit)
                except ValueError:return None
            elif chosen is not None and chosen.lower()!=explicit.lower():return None
            bound[field]=explicit if field!='scope' else explicit.lower()
        elif chosen is not None:
            if field=='scope' and chosen=='retail':continue
            return None
    bound.setdefault('scope','retail')
    if bound.get('locale') is None:bound['locale']='hi-IN' if language=='hi' else 'en-IN'
    return bound
