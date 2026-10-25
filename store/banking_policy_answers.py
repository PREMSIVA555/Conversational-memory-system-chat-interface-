"""INV10: curated claim rules, independent of retrieval rank/citation membership."""
from dataclasses import dataclass
from decimal import Decimal
import re
from store.banking_contracts import Money

DECLINE_SUPPORT_TEXT='Synthetic card-decline support procedure: review the recorded transaction and confirm a saved support preview to create a local support request. This procedure does not determine a decline cause, guarantee payment approval, promise resolution, or establish external delivery.'
DECLINE_SUPPORT_HI='सिंथेटिक कार्ड-अस्वीकृति सहायता प्रक्रिया: दर्ज लेनदेन की जाँच करें और स्थानीय सहायता अनुरोध बनाने के लिए सेव किए गए पूर्वावलोकन की पुष्टि करें। यह प्रक्रिया अस्वीकृति का कारण तय नहीं करती और भुगतान मंज़ूरी, समाधान या बाहरी डिलीवरी का वादा नहीं करती।'


@dataclass
class PolicyAnswer:
    text: str
    evidence: list
    metadata: dict


def localize(text):
    text=text.replace(DECLINE_SUPPORT_TEXT,DECLINE_SUPPORT_HI)
    for source,target in [
        ('The synthetic credit-card annual fee is ', 'सिंथेटिक क्रेडिट कार्ड का वार्षिक शुल्क '),
        ('The synthetic fee-waiver rule requires annual spend of ', 'सिंथेटिक शुल्क-माफी नियम के अनुसार आवश्यक वार्षिक खर्च '),
        ('This policy does not establish why a fee was charged or whether your own spending qualifies for a waiver.', 'यह नीति शुल्क लगने का कारण या आपके खर्च की शुल्क-माफी पात्रता प्रमाणित नहीं करती।'),
        ('No complete applicable policy evidence is available. Please clarify the product, locale or effective date, or contact support.', 'पूर्ण लागू नीति साक्ष्य उपलब्ध नहीं है। कृपया उत्पाद, लोकेल या लागू तारीख स्पष्ट करें, या सहायता से संपर्क करें।'),
        ('The retrieved policy does not support this explanation. Please clarify or contact support.', 'प्राप्त नीति इस स्पष्टीकरण का समर्थन नहीं करती। कृपया स्पष्ट करें या सहायता से संपर्क करें।'),
        ('The retrieved policy context is incomplete. I cannot safely state this rule; please narrow the question or contact support.', 'प्राप्त नीति संदर्भ अधूरा है। नियम की पुष्टि नहीं हो सकती; कृपया प्रश्न सीमित करें या सहायता से संपर्क करें।'),
        ('Applicable policy evidence conflicts. I cannot safely state this rule; please contact support.', 'लागू नीति साक्ष्य में विरोध है। नियम की पुष्टि नहीं हो सकती; कृपया सहायता से संपर्क करें।'),
        ('Retrieval is degraded; this rule is supported by the surviving evidence only.', 'कुछ नीति स्रोत उपलब्ध नहीं हैं; यह नियम केवल उपलब्ध साक्ष्य से समर्थित है।'),
        ('The supplied explanation was unsupported; only checked policy templates or limitations are shown.', 'दिया गया स्पष्टीकरण असमर्थित था; केवल जाँचा गया नीति नियम या सीमा दिखाई गई है।'),
        ('Policy context contains a qualification I cannot safely interpret. Please clarify or contact support.', 'नीति संदर्भ में ऐसी शर्त है जिसकी पुष्टि नहीं हो सकती। कृपया स्पष्ट करें या सहायता से संपर्क करें।'),
        ('Synthetic policy ', 'सिंथेटिक नीति '), ('; effective ', '; लागू अवधि '), (' (end exclusive).', ' (अंतिम समय शामिल नहीं)।')]:
        text=text.replace(source,target)
    return text


def explain(result, topic):
    """Only exact, complete supported clauses entail these narrow templates.

    A related citation, arbitrary model prose, or a partial sentence is never a
    proof. All retrieved clauses for the requested rule must agree. No spending,
    charged-fee cause or personal waiver eligibility is inferred from policy.
    """
    rules = {
        'annual_fee': (r'Synthetic credit card annual fee is INR ([0-9]{1,12})\.',
                       'The synthetic credit-card annual fee is {amount}.'),
        'fee_waiver': (r'Fee waiver requires annual spend INR ([0-9]{1,12})\.',
                       'The synthetic fee-waiver rule requires annual spend of {amount}.'),
    }
    if topic not in (*rules,'decline_support'):raise ValueError('Unknown policy topic')
    metadata=dict(retrieval_status=result['status'],lanes=result.get('lanes',{}),set_id=result.get('set_id'),
        applicability=result['applicability'],effective_at=result['effective_at'],support='unsupported',topic=topic)
    limitation='This policy does not establish why a fee was charged or whether your own spending qualifies for a waiver.'
    if result['status'] not in ('ok','degraded'):
        return PolicyAnswer('No complete applicable policy evidence is available. Please clarify the product, locale or effective date, or contact support.',[],metadata)
    if not result.get('complete',False):
        return PolicyAnswer('The retrieved policy context is incomplete. I cannot safely state this rule; please narrow the question or contact support.',[],metadata)
    supported=[]
    context=result.get('context',result['evidence'])
    if any(e['text']!=DECLINE_SUPPORT_TEXT and not any(re.fullmatch(p,e['text']) for p,_ in rules.values()) for e in context):
        return PolicyAnswer('Policy context contains a qualification I cannot safely interpret. Please clarify or contact support.',[],metadata)
    if topic=='decline_support':
        matches=[e for e in context if e['text']==DECLINE_SUPPORT_TEXT and e['product']=='credit-card']
        if not matches:return PolicyAnswer('The retrieved policy does not support this explanation. Please clarify or contact support.',[],metadata)
        evidence=[dict(ref=result['set_id']+'/'+e['version']+'/'+e['section'],field=topic,value=DECLINE_SUPPORT_TEXT,
            as_of=result['effective_at'],version=e['version'],authority='applicable_synthetic_policy',
            citation={k:e[k] for k in ('version','section','source_hash','product','scope','locale','effective_from','effective_until')},
            source_text=e['text'],set_id=result['set_id']) for e in matches]
        text=DECLINE_SUPPORT_TEXT+'\n'+'\n'.join('Synthetic policy '+e['citation']['version']+' / '+e['citation']['section']+'; effective '+e['citation']['effective_from']+' to '+e['citation']['effective_until']+' (end exclusive).' for e in evidence)
        if result['status']=='degraded':text+='\nRetrieval is degraded; this rule is supported by the surviving evidence only.'
        metadata['support']='supported_template'
        return PolicyAnswer(text,evidence,metadata)
    pattern, template=rules[topic]
    for e in context:
        match=re.fullmatch(pattern,e['text'])
        # The supported rule is explicitly a credit-card/INR rule; text alone
        # cannot authorize a wrong-product policy, even if it is identical.
        if match and e['product']=='credit-card':
            money=Money(minor_units=str(int(Decimal(match[1])*100)),currency='INR',exponent=2)
            supported.append((e,money))
    if not supported:
        return PolicyAnswer('The retrieved policy does not support this explanation. Please clarify or contact support. '+limitation,[],metadata)
    if len({m.minor_units for _,m in supported})!=1:
        return PolicyAnswer('Applicable policy evidence conflicts. I cannot safely state this rule; please contact support.',[],metadata)
    evidence=[]
    for e,m in supported:
        evidence.append(dict(ref=result['set_id']+'/'+e['version']+'/'+e['section'],field=topic,value=m.model_dump(),
            as_of=result['effective_at'],version=e['version'],authority='applicable_synthetic_policy',
            citation={k:e[k] for k in ('version','section','source_hash','product','scope','locale','effective_from','effective_until')},
            source_text=e['text'],set_id=result['set_id']))
    text=template.format(amount=supported[0][1].formatted())+' '+limitation
    text+='\n'+('\n'.join('Synthetic policy '+e['citation']['version']+' / '+e['citation']['section']+'; effective '+e['citation']['effective_from']+' to '+e['citation']['effective_until']+' (end exclusive).' for e in evidence))
    if result['status']=='degraded':text+='\nRetrieval is degraded; this rule is supported by the surviving evidence only.'
    metadata['support']='supported_template'
    return PolicyAnswer(text,evidence,metadata)


class PolicyReads:
    async def explain_policy(self,context,*,product,scope='retail',locale='en-IN',topic='annual_fee',at=None):
        import asyncio
        import psycopg
        from store.banking_policies import retrieve
        from store.banking_contracts import ProviderError
        if not self.policies_enabled:raise ProviderError('unsupported',422)
        await self.database.sessions.revalidate(context)
        # No paid embedding calls in this baseline. Missing vector is reported
        # explicitly; a supported template may use surviving lexical evidence.
        question={'annual_fee':'annual fee','fee_waiver':'waiver annual spend','decline_support':'card decline support procedure'}.get(topic)
        if question is None:raise ProviderError('invalid_input',422)
        def read():
            with psycopg.connect(self.database.sessions.settings.runtime_dsn,autocommit=True,connect_timeout=2) as conn:
                return retrieve(conn,product=product,scope=scope,locale=locale,question=question,at=at,limit=20,include_context=True)
        try:
            async with asyncio.timeout(2):result=await asyncio.to_thread(read)
        except (psycopg.Error,TimeoutError):raise ProviderError('unavailable',503,True) from None
        except ValueError:raise ProviderError('invalid_input',422) from None
        await self.database.sessions.revalidate(context)
        return explain(result,topic)
