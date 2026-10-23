"""Opaque sessions and one-use browser-bound login transactions in Redis."""
from dataclasses import dataclass
import hashlib
import json
import secrets
import time

from fastapi import HTTPException
from redis.asyncio import Redis
from redis.exceptions import RedisError

SID_COOKIE = "__Host-bank_sid"
FLOW_COOKIE = "__Host-bank_login"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class RequestContext:
    customer_id: str
    session_key: str
    csrf: str
    expires_at: float


class Sessions:
    def __init__(self, settings):
        self.settings = settings
        self.redis = Redis.from_url(settings.redis_url, decode_responses=True,
                                    socket_timeout=2, socket_connect_timeout=2)

    def key(self, kind, token):
        return f"{self.settings.namespace}:{kind}:{digest(token)}"

    async def command(self, method, *args, **kwargs):
        try:
            return await getattr(self.redis, method)(*args, **kwargs)
        except RedisError:
            raise HTTPException(503, "Authentication store unavailable") from None

    async def begin(self):
        state, browser = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        record = dict(nonce=secrets.token_urlsafe(32), verifier=secrets.token_urlsafe(48),
                      browser=digest(browser), expires=time.time() + 300)
        await self.command("set", self.key("flow", state), json.dumps(record), ex=300)
        return state, browser, record

    async def consume(self, state, browser):
        if not state or not browser or len(state) > 200 or len(browser) > 200:
            raise HTTPException(401, "Invalid login transaction")
        value = await self.command("getdel", self.key("flow", state))
        record = json.loads(value) if value else None
        if (not record or record["expires"] <= time.time()
                or not secrets.compare_digest(record["browser"], digest(browser))):
            raise HTTPException(401, "Invalid login transaction")
        return record

    async def issue(self, customer, old_token=None):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        record = dict(customer_id=str(customer), csrf=csrf,
                      expires_at=time.time() + self.settings.session_seconds)
        # Atomic rotation: old session disappears in the same Redis transaction.
        try:
            async with self.redis.pipeline(transaction=True) as pipe:
                if old_token:
                    pipe.delete(self.key("session", old_token))
                pipe.set(self.key("session", token), json.dumps(record), ex=self.settings.session_seconds)
                await pipe.execute()
        except RedisError:
            raise HTTPException(503, "Authentication store unavailable") from None
        return token

    async def require(self, token):
        if not token or len(token) > 200:
            raise HTTPException(401, "Authentication required")
        key = self.key("session", token)
        return await self.require_key(key)

    async def require_key(self, key):
        if not key.startswith(self.settings.namespace + ":session:"):
            raise HTTPException(401, "Invalid session")
        value = await self.command("get", key)
        record = json.loads(value) if value else None
        if not record or record["expires_at"] <= time.time():
            raise HTTPException(401, "Session expired")
        return RequestContext(record["customer_id"], key, record["csrf"], record["expires_at"])

    async def revalidate(self, context):
        fresh = await self.require_key(context.session_key)
        if fresh.customer_id != context.customer_id:
            raise HTTPException(401, "Session changed")
        return fresh

    async def revoke(self, context):
        await self.command("delete", context.session_key)

    async def rotate(self, context):
        token = secrets.token_urlsafe(32)
        record = dict(customer_id=context.customer_id, csrf=secrets.token_urlsafe(32),
                      expires_at=time.time() + self.settings.session_seconds)
        result = await self.command('eval', '''
            local old=redis.call('GET',KEYS[1])
            if not old then return 0 end
            local value=cjson.decode(old)
            if value.customer_id~=ARGV[1] or value.expires_at<=tonumber(ARGV[2]) then return 0 end
            redis.call('DEL',KEYS[1])
            redis.call('SET',KEYS[2],ARGV[3],'EX',ARGV[4])
            return 1
            ''', 2, context.session_key, self.key('session',token), context.customer_id,
            time.time(), json.dumps(record), self.settings.session_seconds)
        if result != 1:
            raise HTTPException(401, 'Session changed')
        return token

    async def close(self):
        await self.redis.aclose()
