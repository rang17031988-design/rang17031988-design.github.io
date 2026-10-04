"""Signed analytics-only marker. It grants no account, order or PII access."""
import base64,hashlib,hmac,json,secrets,time

ACTIVATION_TTL=1800
MARKER_TTL=180*86400

def mint(key,kind,now=None):
    if not key or kind not in ('activate','owner'):raise ValueError('marker configuration')
    now=int(time.time() if now is None else now)
    ttl=ACTIVATION_TTL if kind=='activate' else MARKER_TTL
    raw=json.dumps({'v':1,'kind':kind,'iat':now,'exp':now+ttl,'nonce':secrets.token_hex(12)},separators=(',',':')).encode()
    encoded=base64.urlsafe_b64encode(raw).decode().rstrip('=')
    sig=hmac.new(key.encode(),b'posuda-owner-analytics-v1:'+encoded.encode(),hashlib.sha256).hexdigest()
    return encoded+'.'+sig

def valid(token,key,kind,now=None):
    try:
        if not key or not isinstance(token,str) or len(token)>500:return False
        raw,sig=token.split('.')
        expected=hmac.new(key.encode(),b'posuda-owner-analytics-v1:'+raw.encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected,sig):return False
        data=json.loads(base64.urlsafe_b64decode(raw+'='*(-len(raw)%4)))
        now=int(time.time() if now is None else now)
        ttl=ACTIVATION_TTL if kind=='activate' else MARKER_TTL
        return data.get('v')==1 and data.get('kind')==kind and data['iat']<=now<=data['exp'] and data['exp']-data['iat']==ttl
    except (ValueError,KeyError,TypeError):return False

def classify(batch,key):
    if batch.get('is_test') is True or batch.get('is_internal') is True:return 'internal_test'
    return 'owner' if valid(batch.get('owner_marker'),key,'owner') else 'customer'
