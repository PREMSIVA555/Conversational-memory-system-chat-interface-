"""Conversational reads: bounded context and checked facts precede optional wording."""
import asyncio
from datetime import datetime,timezone
import json
import re
import time
from uuid import UUID
from fastapi import HTTPException
from api.privacy import model_input
from llm import config as llm
from graphs.account_coordinator import Plan,TOOLS,EvidenceRegistry,SAFE
from graphs.policy_request import bind_policy_request
from store.banking_runs import RunStore
from store.banking_workspace import Workspace,validate_selection
from store.banking_contracts import ProviderError
from store.banking_policy_answers import PolicyAnswer,localize

UUID_PATTERN=r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
METHODS={'balance':'get_account','card_details':'get_card','loan_details':'get_loan',
         'schedule':'list_emi_schedule','payments':'list_loan_payments'}
TITLES={'account':('your account','आपका खाता'),'card':('your card','आपका कार्ड'),'loan':('your loan','आपका ऋण')}
INTRO={
 'en':["Here’s what I found.","Let’s look at the checked details.","I can help you review these records.","Here are the details for your question."],
 'hi':["यह जानकारी मिली है।","आइए जाँचे गए विवरण देखें।","मैं इन रिकॉर्ड को समझने में आपकी मदद कर सकता हूँ।","आपके प्रश्न का विवरण यहाँ है।"]}


class PlanningUnavailable(Exception):pass
class PolicyClarification(Exception):pass

def bilingual(language,en,hi):return hi if language=='hi' else en


def product(item,kind):
    label=item.display_name
    if kind=='card':label+=f' · {item.kind} •••• {item.last_four}'
    return dict(kind=kind,id=item.id,version=item.source.version,label=label)


def action(label,code,resource=None):
    return dict(label=label,action=code,resource_selection={k:resource[k] for k in ('kind','id','version')} if resource else None)


def validate_wording(raw,language,allowed_actions):
    """Only audited conversational framing can surround immutable financial blocks.

    Arbitrary prose cannot be certified by counting matching numbers/citations.
    The model selects contextual phrasing; it never rewrites financial clauses.
    """
    try:
        value=json.loads(raw)
        if set(value)!= {'intro','follow_up'} or value['intro'] not in INTRO[language]:return None
        if value['follow_up'] is not None and value['follow_up'] not in allowed_actions:return None
        return value
    except (ValueError,TypeError,KeyError):return None


class WorkspaceCoordinator:
    def __init__(self,database,provider,transport=None,*,run_seconds=10):
        self.database=database;self.provider=provider;self.transport=transport
        self.runs=RunStore(database);self.workspace=Workspace(database,provider);self.run_seconds=run_seconds

    async def run(self,context,row,request):
        language=request['instructions'].get('language',request['preferences']['preferred_response_language'])
        detailed=request['instructions'].get('format',request['preferences']['preferred_response_format'])=='detailed'
        text=request['text']; lower=text.casefold().strip();cid=str(row['conversation_id'])
        claim_text=text+('\nSelected account: '+request['selection']['account_id'] if request.get('selection') else '')
        run=await self.runs.claim(context,str(row['run_id']),claim_text,self.run_seconds)
        if run['result'] is not None:return run['result']
        stop=time.monotonic()+max(0,(run['deadline']-datetime.now(timezone.utc)).total_seconds())
        timings={};registry=EvidenceRegistry();resource=None;refs=[];lineage=[]
        result=dict(status='completed',message_kind='answer',text='',blocks=[],actions=[],evidence=[])
        async def dispatch(kind,operation,cap=2):
            if stop-time.monotonic()<=.1:raise TimeoutError()
            n=await self.runs.reserve(context,run,kind)
            if n is None:raise TimeoutError()
            started=time.monotonic()
            try:
                async with asyncio.timeout(min(cap,max(0,stop-time.monotonic()-.1))):value=await operation()
            except Exception:
                await self.runs.record(context,run,n,dict(outcome='failed',cost='unknown',elapsed_ms=round((time.monotonic()-started)*1000,2)))
                raise
            usage=dict(outcome='returned',elapsed_ms=round((time.monotonic()-started)*1000,2))
            if isinstance(value,llm.StructuredCompletion):usage.update(model=value.model,configured_model=value.configured_model,tokens=value.usage)
            await self.runs.record(context,run,n,usage)
            timings[kind+'_ms']=timings.get(kind+'_ms',0)+usage['elapsed_ms']
            return value
        async def read(method,**args):return await dispatch('tool',lambda:getattr(self.provider,method)(context,**args))
        def say(en,hi,kind='answer',status='completed'):
            result.update(text=bilingual(language,en,hi),message_kind=kind,status=status)
        try:
            chosen=request.get('resource_selection')
            if not chosen and request.get('selection'):
                chosen=dict(kind='account',id=request['selection']['account_id'],version=request['selection']['version'])
            previous=await self.workspace.context(context,cid,row['source_id'])
            def select_product(candidate):
                nonlocal resource,refs,lineage
                resource=candidate
                same=previous and len(previous['lineage'])<20 and all(previous['resource'][k]==candidate[k] for k in ('kind','id'))
                refs=previous['refs'] if same else []
                lineage=[str(x) for x in previous['lineage']] if same else []
            explicit_ids=re.findall(UUID_PATTERN,lower)
            if chosen:
                try:label=await validate_selection(self.database,context,chosen)
                except HTTPException as error:
                    if error.status_code not in (404,409):raise
                    raise ProviderError('selection_changed',409) from None
                select_product(dict(chosen,label=label))
            elif previous and len(previous['lineage'])<20:
                select_product(previous['resource'])
            if re.fullmatch(r'(hi|hello|hey|good morning|good evening|नमस्ते|नमस्कार|हाय)[! .।]*',lower):
                say('Hi! I can help you check your accounts, cards and loans. What would you like to see?',
                    'नमस्ते! मैं आपके खाते, कार्ड और ऋण की जानकारी देखने में मदद कर सकता हूँ। आप क्या देखना चाहेंगे?','greeting')
                result['actions']=[action(bilingual(language,'My balance','मेरा बैलेंस'),'balance'),action(bilingual(language,'My cards','मेरे कार्ड'),'products',None),action(bilingual(language,'Loan payments','ऋण भुगतान'),'payments')]
            elif re.fullmatch(r'(thanks|thank you|धन्यवाद)[! .।]*',lower):
                say('You’re welcome. I’m here when you need to check something else.','आपका स्वागत है। अन्य जानकारी चाहिए तो बताइए।','greeting')
            elif re.fullmatch(r'(help|what can you do|मदद)[? .!।]*',lower) or request.get('action')=='help':
                say('I can check balances, transactions, card limits, loan schedules and payment records. Pick a product or ask a question. For support, use Get help with this below an answer to prepare and review your request. Transfers are not available.',
                    'मैं बैलेंस, लेनदेन, कार्ड सीमा, ऋण अनुसूची और भुगतान रिकॉर्ड देख सकता हूँ। उत्पाद चुनें या प्रश्न पूछें। सहायता अनुरोध तैयार करने और जाँचने के लिए उत्तर के नीचे इस बारे में सहायता लें चुनें। ट्रांसफर उपलब्ध नहीं हैं।','help')
            elif re.search(r'\b(transfer|send money|pay now|block my|unblock|change.*limit|submit.*case)\b|पैसे भेज|ट्रांसफर',lower):
                say('I can help you review information, but I can’t perform that action.','मैं जानकारी देखने में मदद कर सकता हूँ, लेकिन यह कार्रवाई नहीं कर सकता।','help','unsupported');resource=None;lineage=[]
            elif re.search(r'\b(case|complaint|support request)\b.*\b(status|update)\b|\b(status|update)\b.*\b(case|complaint|support request)\b|(?:शिकायत|सहायता अनुरोध|केस).*(?:स्थिति|अपडेट)|(?:स्थिति|अपडेट).*(?:शिकायत|सहायता अनुरोध|केस)',lower) or re.fullmatch(r'sr-[0-9a-f]{32}[? .!]*',lower):
                resource=None;lineage=[]
                reference=re.findall(r'\bsr-([0-9a-f]{32})\b',lower)
                if len(reference)>1:raise ValueError('Choose one support request')
                if reference:case_id=str(UUID(hex=reference[0]))
                else:
                    page=await read('list_support_cases')
                    if len(page['items'])!=1 or page['next_cursor']:
                        say('Choose a request in Support requests to check its current status.' if page['items'] else 'You have no support requests yet.',
                            'वर्तमान स्थिति के लिए सहायता अनुरोध सूची में एक अनुरोध चुनें।' if page['items'] else 'अभी आपका कोई सहायता अनुरोध नहीं है।','clarification','clarify' if page['items'] else 'no_evidence')
                        case_id=None
                    else:case_id=str(page['items'][0]['id'])
                if case_id:
                    current=await read('get_support_case',case_id=case_id)
                    en,hi={'created':('received','प्राप्त है'),'in_review':('in review','जाँच में है'),'resolved':('resolved','का समाधान हो गया है')}[current['status']]
                    say(f"Your support request {current['case_number']} is {en}.",f"आपका सहायता अनुरोध {current['case_number']} {hi}।")
                    result['blocks']=[dict(kind='support_case',title=bilingual(language,'Current support status','सहायता अनुरोध की वर्तमान स्थिति'),data=current)]
                    result['evidence']=[dict(ref=current['id'],field='status',value=current['status'],as_of=current['checked_at'],version=current['status_updated_at'],authority='fresh_authorized_support')]
            elif re.fullmatch(r'(reply in (hindi|english)|be (concise|detailed)|हिंदी में जवाब दो|अंग्रेजी में जवाब दो)[.।]*',lower):
                hindi='hindi' in lower or 'हिंदी' in lower
                say('Should I use that preference for this conversation or all conversations?','यह पसंद इस बातचीत के लिए रखें या सभी बातचीत के लिए?','clarification','clarify')
                phrase=('Reply in '+('Hindi' if hindi else 'English')+' for this conversation') if 'reply' in lower or 'जवाब' in lower else 'Be '+('detailed' if 'detailed' in lower else 'concise')+' for this conversation'
                all_phrase=('Always reply in '+('Hindi' if hindi else 'English')) if 'reply' in lower or 'जवाब' in lower else 'Always be '+('detailed' if 'detailed' in lower else 'concise')
                result['actions']=[dict(label=bilingual(language,'This conversation','इस बातचीत के लिए'),text=phrase),dict(label=bilingual(language,'All conversations','सभी बातचीत के लिए'),text=all_phrase)]
            else:
                op=request.get('action');kind=None;args={};method=None;ordinal=None
                decline_policy=bool(re.search(r'declin(?:e|ed).*(?:policy|procedure|support)|(?:policy|procedure).*declin|अस्वीकृत.*(?:नीति|प्रक्रिया)',lower))
                policy=decline_policy or bool(re.search(r'annual[ _-]fee|fee[ _-]waiver|वार्षिक शुल्क|शुल्क.?माफी',lower))
                if policy:
                    # The existing applicability binder still controls locale/scope/time.
                    if decline_policy:plan=Plan(tool='explain_policy',args=dict(product='credit-card',topic='decline_support'))
                    else:
                        raw=await self.plan(text,dispatch)
                        plan=Plan.model_validate_json(raw.content)
                    if plan.tool!='explain_policy':raise ValueError('Policy clarification')
                    args=bind_policy_request(text,TOOLS['explain_policy'].model_validate(plan.args).model_dump(exclude_unset=True),language=language)
                    if args is None:raise PolicyClarification()
                    method='explain_policy'
                else:
                    if re.search(r'\bcard\b|\bcards\b|\bdebit\b|\bcredit\b|कार्ड|डेबिट|क्रेडिट',lower):kind='card'
                    elif re.search(r'\bloan\b|\bloans\b|\bemi\b|ऋण|किस्त',lower):kind='loan'
                    elif re.search(r'\baccount\b|\baccounts\b|\bsavings\b|\bcurrent\b|खात|बचत|चालू',lower):kind='account'
                    elif re.search(r'balance|बैलेंस',lower) and not re.search(r'\bits\b|\bthis\b|\bthat\b|इसका|उसका',lower):kind='account'
                    if chosen:kind=chosen['kind'] if kind is None else kind
                    if not kind and resource:kind=resource['kind']
                    if op is None:
                        if re.search(r'\bschedule\b|due|अनुसूची|नियत',lower):op='schedule';kind='loan'
                        elif re.search(r'\bpayments?\b|paid|भुगतान',lower):
                            # Payment is not a product type. Keep an explicit or
                            # validated active product; otherwise ask which one.
                            if kind is None:raise ValueError('Choose an account, card or loan')
                            op='payments' if kind=='loan' else 'transactions'
                        elif re.search(r'transaction|history|लेनदेन',lower):op='transactions'
                        elif re.search(r'balance|बैलेंस',lower):
                            kind=kind or 'account';op={'account':'balance','card':'card_details','loan':'loan_details'}[kind]
                        elif re.search(r'list|show my|my cards|my loans|मेरे|मेरी',lower):op='products'
                        elif kind:op={'account':'balance','card':'card_details','loan':'loan_details'}[kind]
                    if kind is None:
                        if op=='transactions':raise ValueError('Choose an account or card first')
                        raw=await self.plan(text,dispatch);p=Plan.model_validate_json(raw.content)
                        if p.tool in ('unsupported','clarify'):raise ValueError('Unclear request')
                        method=p.tool;args=TOOLS[method].model_validate(p.args).model_dump()
                        kind='card' if 'card' in method else 'loan' if 'loan' in method or 'emi' in method else 'account'
                        op='transactions' if 'transactions' in method else 'payments' if 'payments' in method else 'schedule' if 'schedule' in method else 'products' if method.startswith('list_') else {'account':'balance','card':'card_details','loan':'loan_details'}[kind]
                        # Model-selected IDs are never authority or a hidden selection.
                        args={k:v for k,v in args.items() if not k.endswith('_id')}
                    if kind is None:kind='card' if 'cards' in lower else 'account'
                    if resource and resource['kind']!=kind:resource=None;lineage=[]
                    aliases={'account':{'savings':r'savings|बचत','current':r'current account|चालू'},'card':{'debit':r'debit|डेबिट','credit':r'credit|क्रेडिट'},'loan':{'closed':r'closed|बंद'}}
                    named=[key for key,pattern in aliases[kind].items() if re.search(pattern,lower)]
                    listing=op=='products'
                    if explicit_ids:
                        # Explicit resource UUID overrides a prior tap/context. FK/read checks deny foreign IDs.
                        select_product(dict(kind=kind,id=explicit_ids[0],version='1',label=TITLES[kind][language=='hi']))
                    if not resource or (named and not chosen and not explicit_ids) or listing:
                        page=await read({'account':'list_accounts','card':'list_cards','loan':'list_loans'}[kind])
                        candidates=page.items
                        exact=[x for x in candidates if x.display_name.casefold() in lower]
                        if exact:candidates=exact
                        elif named:candidates=[x for x in candidates if any(n in (x.display_name+' '+getattr(x,'kind','')+' '+getattr(x,'status','')).lower() for n in named)]
                        suffix=re.search(r'(?:ending|ends in|आखिरी)\s*([0-9]{4})',lower)
                        if suffix and kind=='card':candidates=[x for x in candidates if x.last_four==suffix[1]]
                        if len(candidates)==1 and page.complete and not listing:
                            select_product(product(candidates[0],kind))
                        else:
                            code={'account':'balance','card':'card_details','loan':'loan_details'}[kind] if listing else op
                            say('Which '+kind+' would you like to check?' if candidates else 'There are no matching products to show.',
                                'आप कौन सा उत्पाद देखना चाहेंगे?' if candidates else 'कोई मेल खाता उत्पाद नहीं मिला।','clarification','clarify' if candidates else 'no_evidence')
                            result['blocks']=[dict(kind='products',title=bilingual(language,'Your products','आपके उत्पाद'),data=dict(items=[{**x.model_dump(mode='json'),'resource_kind':kind} for x in candidates],complete=page.complete))]
                            result['actions']=[action(product(x,kind)['label'],code,product(x,kind)) for x in candidates]
                            if not page.complete:result['text']+=bilingual(language,' More products are available in My products.',' अन्य उत्पाद मेरे उत्पाद में उपलब्ध हैं।')
                            resource=None;method=None
                    if resource:
                        if not method or method.startswith('list_') and 'transactions' not in method and 'payments' not in method and 'schedule' not in method:
                            method=({'account':'list_transactions','card':'list_card_transactions','loan':'list_loan_payments'}[kind] if op=='transactions' else METHODS.get(op,{'account':'get_account','card':'get_card','loan':'get_loan'}[kind]))
                        args[kind+'_id']=resource['id']
                        if method.startswith('list_'):
                            dates=re.findall(r'\b\d{4}-\d{2}-\d{2}\b',text)
                            if dates:args.update(start=dates[0],end=dates[-1])
                            for status in ('posted','pending','declined','reversed'):
                                if status in lower:args['status']=status
                            match=re.search(r'(first|second|third|fourth|पहला|दूसरा|तीसरा)\s*(?:transaction|record|लेनदेन)|(?:transaction|record|लेनदेन)\s*(first|second|third|fourth|पहला|दूसरा|तीसरा)',lower)
                            if match:
                                word=match[1] or match[2];ordinal={'first':0,'second':1,'third':2,'fourth':3,'पहला':0,'दूसरा':1,'तीसरा':2}[word]
                                if not previous or ordinal>=len(previous['refs']) or previous['resource']['id']!=resource['id']:raise ValueError('Missing result context')
                                target=previous['refs'][ordinal]
                                if target['method']!=method:raise ValueError('Wrong result category')
                                args=target['args'];args[kind+'_id']=resource['id']
                            size=re.search(r'page size (\d+)',lower)
                            if size:args['limit']=min(20,max(1,int(size[1])))
                            cursor=re.search(r'; cursor ([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)',text)
                            if cursor:args['cursor']=cursor[1]
                            args.setdefault('limit',20)
                if method:
                    value=await read(method,**args)
                    text_checked=registry.render(value)
                    if isinstance(value,PolicyAnswer):
                        result['text']=localize(text_checked) if language=='hi' else text_checked
                        result['blocks']=[dict(kind='policy',title=bilingual(language,'Policy details','नीति विवरण'),data=dict(text=result['text']))]
                        result['status']='completed' if value.evidence else 'no_evidence'
                    else:
                        data=value.model_dump(mode='json')
                        if resource:
                            if hasattr(value,'display_name'):resource=product(value,resource['kind'])
                            # A fresh version is fetched on every question; never use a previous balance.
                            if not hasattr(value,'items'):resource['version']=value.source.version
                        title=resource['label'] if resource else TITLES[kind][language=='hi']
                        if hasattr(value,'items'):
                            rows=value.items
                            if ordinal is not None:
                                rows=[x for x in rows if x.id==target['id']]
                                if not rows:raise ValueError('Result changed')
                            data['items']=[x.model_dump(mode='json') for x in rows]
                            refs=[dict(id=x.id,method=method,args=args) for x in rows][:20]
                        blockkind={'get_account':'account','get_card':'card','get_loan':'loan','list_transactions':'transactions','list_card_transactions':'transactions','list_emi_schedule':'schedule','list_loan_payments':'payments'}[method]
                        result['blocks']=[dict(kind=blockkind,title=title,data=data)]
                        say('Here are the checked details for '+title+'.','यह '+title+' का जाँचा गया विवरण है।')
                        if blockkind in ('schedule','payments'):
                            result['text']+=bilingual(language,' A schedule is not proof of payment. Pending, declined and reversed records do not confirm posted payment.',' अनुसूची भुगतान का प्रमाण नहीं है। pending, declined और reversed रिकॉर्ड posted भुगतान की पुष्टि नहीं करते।')
                        if re.search(r'why|क्यों|कारण',lower):result['text']+=bilingual(language,' The checked records do not establish the cause.',' जाँचे गए रिकॉर्ड से कारण की पुष्टि नहीं होती।')
                        if hasattr(value,'items') and not value.items:result['status']='no_evidence'
                        if resource:
                            if kind in ('account','card') and op=='transactions':
                                result['actions']=[action(bilingual(language,'Choose another '+kind,'दूसरा उत्पाद चुनें'),'products')]
                                # The label names the category in both languages;
                                # the next turn still discovers owned choices.
                                result['actions'][0]['text']='List my '+kind+'s'
                            else:
                                nextcode='payments' if kind=='loan' and op=='schedule' else 'transactions' if kind in ('account','card') else 'schedule'
                                result['actions']=[action(bilingual(language,{'payments':'View payment records','transactions':'Show transactions','schedule':'EMI schedule'}[nextcode],{'payments':'भुगतान रिकॉर्ड देखें','transactions':'लेनदेन देखें','schedule':'EMI अनुसूची'}[nextcode]),nextcode,resource)]
                        if getattr(value,'next_cursor',None):
                            continuation=f'Show {"card transactions" if kind=="card" else "account transactions" if kind=="account" else "actual loan payments" if method=="list_loan_payments" else "EMI schedule"} for {resource["id"]} from {value.start} to {value.end}; page size {args["limit"]}; cursor {value.next_cursor}'
                            result['next_question']=continuation
                    result['evidence']=registry.claims
                    # The new synthetic procedure already has an exact checked
                    # explanation; it does not need optional model framing.
                    if not decline_policy and (detailed or re.search(r'why|explain|समझा|क्यों',lower)):
                        started=time.monotonic()
                        try:
                            if stop-time.monotonic()>1:
                                allowed=[x['action'] for x in result['actions'] if x.get('action')]
                                prompt=[{'role':'system','content':'Return JSON {"intro":one exact supplied phrase,"follow_up":one allowed action or null}. Choose friendly framing only. Never write financial claims or change facts.'},
                                    {'role':'user','content':json.dumps(dict(language=language,phrases=INTRO[language],allowed_actions=allowed,message_kind='checked_banking_result',read_operation=method,result_status=result['status'],fact_blocks=[b['kind'] for b in result['blocks']]))}]
                                raw=await dispatch('model',lambda:llm.structured_completion(prompt,timeout=min(2,max(.1,stop-time.monotonic()-.2)),transport=self.transport),2)
                                accepted=validate_wording(raw.content,language,allowed)
                                if accepted:
                                    result['intro']=accepted['intro'];result['wording_status']='checked'
                                    result['actions']=[a for a in result['actions'] if a.get('action')==accepted['follow_up']] if accepted['follow_up'] else []
                                else:result['wording_status']='fallback'
                        except Exception:result['wording_status']='fallback'
                        timings['wording_ms']=round((time.monotonic()-started)*1000,2)
        except HTTPException:
            raise
        except ProviderError as error:
            if error.status==401:raise HTTPException(401) from None
            say('That information isn’t available right now. Please refresh your products and try again.','अभी यह जानकारी उपलब्ध नहीं है। उत्पाद रीफ़्रेश करके फिर कोशिश करें।','answer','no_evidence' if error.code=='not_found' else 'unavailable')
            result.update(blocks=[],actions=[],evidence=[]);resource=None
        except PolicyClarification:
            say('Please specify the policy product and locale. If you supply an effective date, include its time and timezone.',
                'कृपया नीति का उत्पाद और लोकेल बताएँ। लागू तारीख देने पर समय और टाइमज़ोन भी लिखें।','clarification','clarify')
            result.update(blocks=[],actions=[],evidence=[]);resource=None
        except PlanningUnavailable:
            say('I can’t safely interpret that question right now. Please use a product action or try again.','अभी इस प्रश्न को सुरक्षित ढंग से समझना संभव नहीं है। उत्पाद का विकल्प चुनें या फिर कोशिश करें।','answer','unavailable')
            result.update(blocks=[],actions=[],evidence=[]);resource=None
        except (ValueError,KeyError,TypeError,AttributeError):
            say('I can help with accounts, cards and loans. Choose a product below, or tell me what you’d like to check.','मैं खाते, कार्ड और ऋण की जानकारी में मदद कर सकता हूँ। उत्पाद चुनें या बताइए क्या देखना है।','clarification','clarify')
            result.update(blocks=[],actions=[],evidence=[]);resource=None
        except (TimeoutError,asyncio.TimeoutError):
            say(SAFE['expired'],'इस प्रश्न की समय सीमा पूरी हो गई। कृपया नया प्रश्न पूछें।','answer','expired');resource=None
        except Exception:
            # SDK/provider faults terminate this accepted turn; reclaiming the
            # worker lease must not create unbounded duplicate model attempts.
            say('That information isn’t available right now. Please use a product action or try again.',
                'अभी यह जानकारी उपलब्ध नहीं है। उत्पाद का विकल्प चुनें या फिर कोशिश करें।','answer','unavailable')
            result.update(blocks=[],actions=[],evidence=[]);resource=None
        await self.database.sessions.revalidate(context)
        if resource is None:refs=[];lineage=[]
        result['timings']=timings
        result['workspace']=dict(resource=resource,refs=refs,lineage=lineage)
        if not await self.runs.publish(context,run,result):return dict(status='expired',text=SAFE['expired'],evidence=[])
        return result

    async def plan(self,text,dispatch):
        async def operation():
            try:minimized=await asyncio.to_thread(model_input,text)
            except Exception:raise PlanningUnavailable() from None
            prompt=[{'role':'system','content':'Return one JSON plan {"tool":registered read name,"args":{}}. Allowed: '+','.join(TOOLS)+',clarify,unsupported. Only read enquiries. No mutations, identity or SQL. Policy requires product credit-card and topic annual_fee, fee_waiver or decline_support; copy explicit locale/scope/ISO at exactly. Never infer personal waiver eligibility.'}, {'role':'user','content':minimized}]
            return await llm.structured_completion(prompt,timeout=5,transport=self.transport)
        return await dispatch('model',operation,5)
