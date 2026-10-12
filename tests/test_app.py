"""Real local tests. Mocked HTTP contracts do NOT establish live BTCPay integration."""
import hashlib
import hmac
import json
import shutil
import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import BTCPayProvider, Database, Settings, btc, create_app, fixture_address, sats, validate_address


def client(tmp_path, **kw):
    s = Settings(database=str(tmp_path/'test.sqlite'), **kw)
    return TestClient(create_app(s), base_url='http://127.0.0.1')


def invoice(c, amount=10000):
    r = c.post('/api/invoices', json={'amount_sats': amount})
    assert r.status_code == 201
    return r.json()


@pytest.mark.parametrize('value', [True, False, 100.0, 1.5, '1000', 99, 10000001, -1, None])
def test_invalid_amounts_do_not_create_invoice(tmp_path, value):
    c = client(tmp_path)
    assert c.post('/api/invoices', json={'amount_sats': value}).status_code == 422
    assert len(c.get('/api/export.csv').text.splitlines()) == 1


@pytest.mark.parametrize('amount', [100, 10000, 10000000])
def test_integer_boundaries_and_matching_qr_uri(tmp_path, amount):
    c = client(tmp_path)
    result = invoice(c, amount)
    assert result['payment_uri'] == 'bitcoin:'+result['address']+'?amount='+btc(amount)
    assert result['address'].startswith('bcrt1')
    assert result['synthetic'] and not result['confirmed']
    r = c.get('/api/invoices/'+result['public_id']+'/qr.svg')
    assert r.status_code == 200 and 'image/svg+xml' in r.headers['content-type']
    assert '<svg' in r.text and '<path' in r.text


@pytest.mark.parametrize('scenario,status,extra,paid,confirmed', [
    ('new','New','None',0,False), ('processing','Processing','None',10000,False),
    ('settled','Settled','None',10000,True), ('partial','New','PaidPartial',5000,False),
    ('over','Settled','PaidOver',20000,True), ('late','Settled','PaidLate',10000,True),
    ('expired','Expired','None',0,False), ('invalid','Invalid','Invalid',0,False),
    ('marked','Settled','Marked',0,False), ('reorg','Processing','None',10000,False)])
def test_synthetic_cases(tmp_path, scenario, status, extra, paid, confirmed):
    c = client(tmp_path)
    i = invoice(c)['public_id']
    r = c.post('/api/demo/'+i, json={'scenario':scenario}).json()
    assert (r['status'],r['additional_status'],r['paid_sats'],r['confirmed']) == (status,extra,paid,confirmed)
    assert r['synthetic'] is True


def test_regression_and_repeat_not_counted_twice(tmp_path):
    c = client(tmp_path)
    i = invoice(c)['public_id']
    for _ in range(3):
        c.post('/api/demo/'+i,json={'scenario':'settled'})
    assert len(c.get('/api/export.csv').text.splitlines()) == 2
    assert c.post('/api/demo/'+i,json={'scenario':'reorg'}).json()['confirmed'] is False
    assert ',Processing,' in c.get('/api/export.csv').text


def test_backup_restore_and_restart_preserve_demo(tmp_path):
    c = client(tmp_path)
    i = invoice(c)['public_id']
    c.post('/api/demo/'+i,json={'scenario':'partial'})
    backup = tmp_path/'backup.sqlite'
    c.app.state.db.backup(str(backup))
    restored = tmp_path/'restored.sqlite'
    shutil.copyfile(backup, restored)
    c2 = TestClient(create_app(Settings(database=str(restored))),base_url='http://127.0.0.1')
    assert c2.get('/api/export.csv').text == c.get('/api/export.csv').text
    assert c2.get('/api/invoices/'+i).json()['paid_sats']==5000
    with pytest.raises(ValueError):
        c.app.state.db.backup(c.app.state.db.path)


def test_limits_unknown_invoice_and_unsupported_fields(tmp_path):
    c = client(tmp_path)
    assert c.get('/api/invoices/guess').status_code == 404
    assert c.post('/api/invoices',json={'amount_sats':100,'private_key':'not-accepted'}).status_code==422
    for _ in range(12): invoice(c,100)
    assert c.post('/api/invoices',json={'amount_sats':100}).status_code==429


def test_browser_boundary_and_no_external_assets(tmp_path):
    c=client(tmp_path)
    assert c.post('/api/invoices',json={'amount_sats':100},headers={'Origin':'https://bad.example'}).status_code==403
    assert c.post('/api/invoices',content='amount=100').status_code==415
    assert c.post('/api/invoices',content='x'*20000,headers={'Content-Type':'application/json'}).status_code==413
    assert c.post('/api/invoices',json={'amount_sats':100},headers={'Content-Length':'wrong'}).status_code==400
    assert c.get('/',headers={'Host':'bad.example'}).status_code==403
    r=c.get('/')
    assert "script-src 'self'" in r.headers['content-security-policy']
    assert 'https://' not in r.text


@pytest.mark.parametrize('value',['NaN','Infinity','-1','1e-8','0.000000001','21000001','1.123456789',1.0])
def test_provider_amount_parser_rejects_invalid_precision(value):
    with pytest.raises(ValueError): sats(value)


def test_checksum_and_mainnet_disabled():
    a=fixture_address(1)
    assert validate_address(a,'regtest')==a
    with pytest.raises(ValueError): validate_address(a[:-1]+('q' if a[-1]!='q' else 'p'),'regtest')
    with pytest.raises(ValueError): validate_address(a,'testnet')
    with pytest.raises(ValueError): Settings(network='mainnet').validate()
    with pytest.raises(ValueError): Settings(mode='btcpay',network='regtest',url='http://remote.example').validate()


class ContractServer:
    def __init__(self):
        self.calls=[]
        self.obj={'id':'contractInvoice','storeId':'store1','currency':'BTC','amount':'0.00010000',
                  'paidAmount':'0.00000000','status':'New','additionalStatus':'None',
                  'checkoutLink':'https://processor.example/i/contractInvoice'}
        self.methods=[{'paymentMethodId':'BTC-CHAIN','destination':fixture_address('contract')}]
        self.failure=False

    def handle(self,req):
        self.calls.append(req)
        assert req.headers['authorization']=='token not-a-real-api-key'
        if self.failure:
            return httpx.Response(503,json={'error':'not-a-real-api-key should never be exposed'})
        if req.method=='POST':
            b=json.loads(req.content)
            assert b['currency']=='BTC' and b['amount']=='0.00010000'
            assert b['checkout']['paymentTolerance']==0
            assert b['checkout']['paymentMethods']==['BTC-CHAIN']
            assert req.url.path=='/api/v1/stores/store1/invoices'
            return httpx.Response(200,json=self.obj)
        if req.url.path.endswith('/payment-methods'):
            assert req.url.params['includeSensitive']=='false'
            assert req.url.params['onlyAccountedPayments']=='true'
            return httpx.Response(200,json=self.methods)
        assert req.url.path=='/api/v1/invoices/contractInvoice'
        return httpx.Response(200,json=self.obj)


def contract_client(tmp_path):
    s=Settings(mode='btcpay',network='regtest',database=str(tmp_path/'contract.sqlite'),
               url='https://processor.example',store='store1',api_key='not-a-real-api-key',
               webhook_secret='w'*40,admin_key='a'*40)
    server=ContractServer()
    provider=BTCPayProvider(s,transport=httpx.MockTransport(server.handle))
    c=TestClient(create_app(s,provider),base_url='http://127.0.0.1')
    return c,server,s


def signed_event(c,s,**changes):
    data={'deliveryId':'delivery1','originalDeliveryId':'delivery1','type':'InvoiceSettled',
          'storeId':'store1','invoiceId':'contractInvoice','paidAmount':'999999','status':'Settled'}
    data.update(changes)
    raw=json.dumps(data).encode()
    signature='sha256='+hmac.new(s.webhook_secret.encode(),raw,hashlib.sha256).hexdigest()
    return c.post('/api/webhook',content=raw,headers={'BTCPay-Sig':signature,'Content-Type':'application/json'})


def test_contract_endpoints_and_secret_redaction(tmp_path):
    c,server,s=contract_client(tmp_path)
    i=invoice(c)
    assert not i['synthetic'] and i['checkout_link'].startswith(s.url)
    assert s.api_key not in json.dumps(i)
    assert 'xpub' not in json.dumps(i)
    assert c.post('/api/demo/'+i['public_id'],json={'scenario':'settled'}).status_code==404
    assert c.get('/api/export.csv').status_code==401
    assert c.get('/api/export.csv',headers={'Authorization':'Bearer '+s.admin_key}).status_code==200
    server.failure=True
    r=c.get('/api/invoices/'+i['public_id'])
    assert r.status_code==503 and s.api_key not in r.text


def test_signed_payload_is_not_payment_proof_and_retry_is_idempotent(tmp_path):
    c,server,s=contract_client(tmp_path)
    i=invoice(c)['public_id']
    assert c.post('/api/webhook',json={'status':'Settled'}).status_code==401
    # A signed event lies about payment. The authoritative mocked response stays New.
    assert signed_event(c,s).json()['status']=='New'
    assert c.get('/api/invoices/'+i).json()['confirmed'] is False
    server.obj.update(status='Settled',paidAmount='0.00010000')
    r=signed_event(c,s,deliveryId='retry1')
    assert r.json()['duplicate'] is True and r.json()['status']=='Settled'
    with c.app.state.db.connect() as db:
        assert db.execute('SELECT count(*) FROM deliveries').fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM invoices').fetchone()[0]==1
    assert signed_event(c,s,storeId='other-store').status_code==422


def test_failed_refresh_can_be_retried_without_consuming_delivery(tmp_path):
    c,server,s=contract_client(tmp_path)
    invoice(c)
    server.failure=True
    assert signed_event(c,s).status_code==503
    with c.app.state.db.connect() as db:
        assert db.execute('SELECT count(*) FROM deliveries').fetchone()[0]==0
    server.failure=False
    assert signed_event(c,s).json()['duplicate'] is False


@pytest.mark.parametrize('field,value', [('storeId','other'),('currency','USD'),('amount','0.00010001'),
                                        ('status','Unknown'),('additionalStatus','Unknown'),
                                        ('checkoutLink','https://bad.example/i/1')])
def test_provider_mismatch_is_rejected(tmp_path,field,value):
    c,server,s=contract_client(tmp_path)
    server.obj[field]=value
    assert c.post('/api/invoices',json={'amount_sats':10000}).status_code==502


def test_wrong_address_and_duplicate_method_are_rejected(tmp_path):
    c,server,s=contract_client(tmp_path)
    server.methods[0]['destination']='bc1qmainnetdestination'
    assert c.post('/api/invoices',json={'amount_sats':10000}).status_code==502
    server.methods=[{'paymentMethodId':'BTC-CHAIN','destination':fixture_address(1)}]*2
    assert c.post('/api/invoices',json={'amount_sats':10000}).status_code==502


def test_marked_or_partial_settlement_is_not_confirmation(tmp_path):
    c,server,s=contract_client(tmp_path)
    i=invoice(c)['public_id']
    server.obj.update(status='Settled',additionalStatus='Marked',paidAmount='0.00010000')
    assert c.get('/api/invoices/'+i).json()['confirmed'] is False
    server.obj.update(additionalStatus='None',paidAmount='0.00005000')
    assert c.get('/api/invoices/'+i).json()['confirmed'] is False


def test_stable_contract_schema_snapshot():
    source=json.loads((Path(__file__).parent.parent/'docs'/'api-contract.json').read_text())
    assert source['create_path']=='/api/v1/stores/{storeId}/invoices'
    assert source['read_path']=='/api/v1/invoices/{invoiceId}'
    assert source['signature']=='sha256=HMAC256(UTF8(secret), raw_body)'
