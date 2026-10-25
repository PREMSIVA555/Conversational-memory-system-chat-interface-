"""Owned current-case reads for checked conversation replies; no model writer."""
from datetime import datetime
from fastapi import HTTPException
from store.banking_contracts import ProviderError
from store.banking_continuity import Continuity

class SupportReads:
    async def list_support_cases(self,context):
        if not self.support_enabled:raise ProviderError('unsupported',422)
        return await Continuity(self.database).cases(context,limit=2)

    async def get_support_case(self,context,case_id):
        if not self.support_enabled:raise ProviderError('unsupported',422)
        try:value=await Continuity(self.database).case(context,case_id)
        except HTTPException as error:
            if error.status_code==404:raise ProviderError('not_found',404) from None
            raise
        return {k:v.isoformat() if isinstance(v,datetime) else str(v) if k=='id' else v for k,v in value.items()}
