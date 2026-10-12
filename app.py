"""Creator Rail: local, non-custodial Bitcoin prototype; demo is synthetic."""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
import qrcode
import qrcode.image.svg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from starlette.concurrency import run_in_threadpool

ROOT = Path(__file__).parent
STATUS = {'New', 'Processing', 'Settled', 'Expired', 'Invalid'}
EXTRA = {'None', 'PaidLate', 'PaidOver', 'PaidPartial', 'Marked', 'Invalid'}
CHARS = 'qpzry9x8gf2tvdw0s3jn54khce6mua7l'


def polymod(values):
    chk = 1
    for value in values:
        top = chk >> 25
        chk = ((chk & 0x1ffffff) << 5) ^ value
        for i, generator in enumerate([0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3]):
            if (top >> i) & 1:
                chk ^= generator
    return chk


def expand(hrp):
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def fixture_address(index):
    # Public synthetic witness programs, NOT wallets and NOT spendable test funds.
    raw = hashlib.sha256(f'creator-rail-fixture-{index}'.encode()).digest()[:20]
    values, acc, bits = [0], 0, 0
    for byte in raw:
        acc = (acc << 8) | byte
        bits += 8
        while bits >= 5:
            bits -= 5
            values.append((acc >> bits) & 31)
    if bits:
        values.append((acc << (5-bits)) & 31)
    chk = polymod(expand('bcrt') + values + [0]*6) ^ 1
    checksum = [(chk >> (5*(5-i))) & 31 for i in range(6)]
    return 'bcrt1' + ''.join(CHARS[v] for v in values + checksum)


def validate_address(address, network):
    """Only checksummed native witness-v0 P2WPKH for this initial scope."""
    hrp = 'bcrt' if network == 'regtest' else 'tb'
    if not isinstance(address, str) or address != address.lower() or not address.startswith(hrp+'1'):
        raise ValueError('Unexpected address network or format')
    if len(address) != len(hrp)+1+39:
        raise ValueError('Only witness-v0 P2WPKH is supported')
    try:
        values = [CHARS.index(c) for c in address[len(hrp)+1:]]
    except ValueError:
        raise ValueError('Invalid address characters') from None
    if values[0] != 0 or polymod(expand(hrp)+values) != 1:
        raise ValueError('Invalid address checksum')
    # 32 five-bit groups encode exactly 20 bytes; there are no padding bits.
    return address


def sats(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{1,8}(?:\.\d{1,8})?', value):
        raise ValueError('Invalid Bitcoin decimal string')
    try:
        result = Decimal(value) * 100_000_000
    except InvalidOperation:
        raise ValueError('Invalid amount') from None
    if result != result.to_integral_value() or result > 2_100_000_000_000_000:
        raise ValueError('Invalid amount precision or range')
    return int(result)


def btc(value):
    return f'{value // 100_000_000}.{value % 100_000_000:08d}'


def payment_uri(address, amount, network):
    validate_address(address, network)
    return f'bitcoin:{address}?amount={btc(amount)}'


@dataclass
class Settings:
    mode: str = 'demo'
    network: str = 'regtest'
    database: str = str(ROOT / 'data' / 'creator.sqlite3')
    url: str = ''
    store: str = ''
    api_key: str = ''
    webhook_secret: str = ''
    admin_key: str = ''

    @classmethod
    def env(cls):
        return cls(mode=os.getenv('CREATOR_MODE', 'demo'), network=os.getenv('CREATOR_NETWORK', 'regtest'),
                   database=os.getenv('CREATOR_DATABASE', cls.database), url=os.getenv('BTCPAY_URL', ''),
                   store=os.getenv('BTCPAY_STORE_ID', ''), api_key=os.getenv('BTCPAY_API_KEY', ''),
                   webhook_secret=os.getenv('BTCPAY_WEBHOOK_SECRET', ''), admin_key=os.getenv('CREATOR_ADMIN_KEY', ''))

    def validate(self):
        if self.mode not in {'demo', 'btcpay'} or self.network not in {'regtest', 'testnet', 'signet'}:
            raise ValueError('Only demo or BTCPay experimental Bitcoin networks are supported; mainnet is disabled')
        if self.mode == 'btcpay':
            u = urlsplit(self.url)
            if not u.hostname or u.username or u.password or u.query or u.fragment or u.path not in {'', '/'}:
                raise ValueError('BTCPay URL must be an origin without credentials or a path')
            if u.scheme != 'https' and not (u.scheme == 'http' and u.hostname in {'127.0.0.1', 'localhost', '::1'}):
                raise ValueError('Remote BTCPay needs HTTPS')
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', self.store):
                raise ValueError('Invalid store ID')
            if not self.api_key or len(self.webhook_secret) < 32 or len(self.admin_key) < 32:
                raise ValueError('BTCPay mode requires configured credentials and 32-character secrets')


class Database:
    def __init__(self, path):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS invoices (
                    public_id TEXT PRIMARY KEY, provider_id TEXT UNIQUE NOT NULL,
                    amount INTEGER NOT NULL, snapshot TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS deliveries (
                    event_id TEXT PRIMARY KEY, invoice_id TEXT NOT NULL);
            ''')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def save(self, public_id, snapshot):
        with self.connect() as db:
            db.execute('INSERT INTO invoices VALUES(?,?,?,?)',
                       (public_id, snapshot['id'], snapshot['amount_sats'], json.dumps(snapshot)))

    def get(self, public_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM invoices WHERE public_id=?', (public_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'Invoice not found')
        return row

    def by_provider(self, provider_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM invoices WHERE provider_id=?', (provider_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'Untracked invoice')
        return row

    def update(self, public_id, snapshot):
        with self.connect() as db:
            db.execute('UPDATE invoices SET snapshot=? WHERE public_id=?', (json.dumps(snapshot), public_id))

    def delivery(self, event_id, invoice_id):
        with self.connect() as db:
            old = db.execute('SELECT invoice_id FROM deliveries WHERE event_id=?', (event_id,)).fetchone()
            if old and old['invoice_id'] != invoice_id:
                raise HTTPException(409, 'Conflicting delivery identity')
            if old:
                return False
            db.execute('INSERT INTO deliveries VALUES(?,?)', (event_id, invoice_id))
        return True

    def export(self, synthetic):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM invoices ORDER BY rowid').fetchall()
        out = io.StringIO(newline='')
        writer = csv.writer(out)
        writer.writerow(['public_id', 'network', 'requested_sats', 'observed_sats', 'status', 'additional_status', 'synthetic'])
        for row in rows:
            s = json.loads(row['snapshot'])
            writer.writerow([row['public_id'], s['network'], row['amount'], s['paid_sats'], s['status'], s['additional_status'], synthetic])
        return out.getvalue()

    def backup(self, path):
        if Path(path).resolve() == Path(self.path).resolve():
            raise ValueError('Backup path must differ from source')
        with self.connect() as source, sqlite3.connect(path) as dest:
            source.backup(dest)
            if dest.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Backup integrity check failed')


class DemoProvider:
    def __init__(self):
        self.records = {}
        self.lock = threading.Lock()

    def create(self, amount):
        with self.lock:
            pid = 'demo' + secrets.token_hex(12)
            result = dict(id=pid, amount_sats=amount, paid_sats=0, status='New', additional_status='None',
                          address=fixture_address(pid), network='regtest', checkout_link=None)
            self.records[pid] = result
            return result.copy()

    def read(self, pid, amount):
        with self.lock:
            return self.records[pid].copy()

    def simulate(self, pid, scenario):
        states = {'new': ('New', 'None', 0), 'processing': ('Processing', 'None', 1),
                  'settled': ('Settled', 'None', 1), 'partial': ('New', 'PaidPartial', .5),
                  'over': ('Settled', 'PaidOver', 2), 'late': ('Settled', 'PaidLate', 1),
                  'expired': ('Expired', 'None', 0), 'invalid': ('Invalid', 'Invalid', 0),
                  'marked': ('Settled', 'Marked', 0), 'reorg': ('Processing', 'None', 1)}
        if scenario not in states:
            raise HTTPException(422, 'Unknown scenario')
        with self.lock:
            row = self.records[pid]
            status, extra, factor = states[scenario]
            # Demo arithmetic stays integer; no monetary float calculations.
            paid = row['amount_sats']//2 if factor == .5 else row['amount_sats']*int(factor)
            row.update(status=status, additional_status=extra, paid_sats=paid)


class BTCPayProvider:
    def __init__(self, settings, transport=None):
        self.s = settings
        self.client = httpx.Client(base_url=settings.url.rstrip('/'), timeout=10, follow_redirects=False,
                                   headers={'Authorization': 'token ' + settings.api_key}, transport=transport,
                                   trust_env=False)

    def request(self, method, path, **kwargs):
        try:
            r = self.client.request(method, path, **kwargs)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError):
            raise HTTPException(503, 'Payment provider unavailable; payment is not confirmed') from None

    def normalize(self, obj, methods, requested):
        try:
            pid = obj['id']
            if not isinstance(pid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', pid):
                raise ValueError('Invalid invoice identifier')
            if obj['storeId'] != self.s.store or obj['currency'] != 'BTC' or sats(obj['amount']) != requested:
                raise ValueError('Invoice does not match store or amount')
            if obj['status'] not in STATUS or obj['additionalStatus'] not in EXTRA:
                raise ValueError('Unsupported state')
            choices = [m for m in methods if m.get('paymentMethodId') == 'BTC-CHAIN']
            if len(choices) != 1:
                raise ValueError('Bitcoin on-chain method missing or ambiguous')
            address = validate_address(choices[0]['destination'], self.s.network)
            checkout = obj['checkoutLink']
            origin = urlsplit(checkout)
            if (origin.scheme, origin.netloc) != (urlsplit(self.s.url).scheme, urlsplit(self.s.url).netloc):
                raise ValueError('Checkout origin changed')
            return dict(id=pid, amount_sats=requested, paid_sats=sats(obj['paidAmount']), status=obj['status'],
                        additional_status=obj['additionalStatus'], address=address, network=self.s.network,
                        checkout_link=checkout)
        except (KeyError, ValueError, TypeError):
            raise HTTPException(502, 'Provider returned an inconsistent or unsupported invoice') from None

    def create(self, amount):
        obj = self.request('POST', '/api/v1/stores/'+quote(self.s.store, safe='')+'/invoices',
                           json={'amount': btc(amount), 'currency': 'BTC', 'checkout': {
                               'paymentMethods': ['BTC-CHAIN'], 'defaultPaymentMethod': 'BTC-CHAIN',
                               'paymentTolerance': 0, 'lazyPaymentMethods': False}})
        pid = obj.get('id', '')
        if not isinstance(pid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', pid):
            raise HTTPException(502, 'Invalid provider invoice identifier')
        methods = self.request('GET', '/api/v1/invoices/'+pid+'/payment-methods',
                               params={'includeSensitive': 'false', 'onlyAccountedPayments': 'true'})
        return self.normalize(obj, methods, amount)

    def read(self, pid, amount):
        obj = self.request('GET', '/api/v1/invoices/'+quote(pid, safe=''))
        methods = self.request('GET', '/api/v1/invoices/'+quote(pid, safe='')+'/payment-methods',
                               params={'includeSensitive': 'false', 'onlyAccountedPayments': 'true'})
        return self.normalize(obj, methods, amount)


class CreateInvoice(BaseModel):
    model_config = ConfigDict(extra='forbid')
    amount_sats: StrictInt = Field(ge=100, le=10_000_000)


class Simulation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    scenario: str = Field(max_length=20)


def create_app(settings=None, provider=None):
    s = settings or Settings.env()
    s.validate()
    if s.mode == 'demo' and s.network != 'regtest':
        raise ValueError('Synthetic fixtures use regtest only')
    api = FastAPI(title='Creator Rail', version='0.1.0', description='Local-only experimental prototype; demo data is synthetic')
    db = Database(s.database)
    p = provider or (DemoProvider() if s.mode == 'demo' else BTCPayProvider(s))
    if s.mode == 'demo' and provider is None:
        with db.connect() as connection:
            for row in connection.execute('SELECT snapshot FROM invoices'):
                record = json.loads(row['snapshot'])
                p.records[record['id']] = record
    api.state.db, api.state.provider, api.state.settings = db, p, s
    limits, limits_lock = defaultdict(deque), threading.Lock()

    @api.middleware('http')
    async def local_boundary(request, call_next):
        if request.url.hostname not in {'127.0.0.1', 'localhost', '::1'}:
            return Response('This prototype is local-only', status_code=403)
        if request.method == 'POST' and request.url.path != '/api/webhook':
            origin = request.headers.get('origin')
            expected = f'{request.url.scheme}://{request.url.netloc}'
            if origin and origin != expected:
                return Response('Cross-origin request rejected', status_code=403)
            if request.headers.get('content-type', '').split(';')[0] != 'application/json':
                return Response('JSON required', status_code=415)
        try:
            content_length = int(request.headers.get('content-length', '0') or 0)
        except ValueError:
            return Response('Invalid content length', status_code=400)
        if content_length < 0:
            return Response('Invalid content length', status_code=400)
        if content_length > 16_384:
            return Response('Request too large', status_code=413)
        if request.method == 'POST' and len(await request.body()) > 16_384:
            return Response('Request too large', status_code=413)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @api.get('/')
    def home():
        return FileResponse(ROOT / 'static' / 'index.html')

    @api.get('/api/profile')
    def profile():
        return {'name': 'Mohammad Javad Cloner', 'mode': s.mode, 'network': s.network,
                'synthetic': s.mode == 'demo', 'currency': 'BTC', 'min_sats': 100, 'max_sats': 10_000_000}

    def view(row, refresh=True):
        if refresh:
            try:
                snapshot = p.read(row['provider_id'], row['amount'])
            except KeyError:
                raise HTTPException(503, 'Synthetic runtime was restarted; create a new demo invoice') from None
            db.update(row['public_id'], snapshot)
        else:
            snapshot = json.loads(row['snapshot'])
        result = {k: v for k, v in snapshot.items() if k != 'id'}
        result['public_id'] = row['public_id']
        result['synthetic'] = s.mode == 'demo'
        result['payment_uri'] = payment_uri(snapshot['address'], row['amount'], s.network)
        result['confirmed'] = (snapshot['status'] == 'Settled' and snapshot['paid_sats'] >= row['amount']
                               and snapshot['additional_status'] in {'None', 'PaidOver', 'PaidLate'})
        return result

    @api.post('/api/invoices', status_code=201)
    def create(body: CreateInvoice, request: Request):
        key = request.client.host if request.client else 'local'
        with limits_lock:
            now = time.monotonic()
            q = limits[key]
            while q and q[0] < now-60:
                q.popleft()
            if len(q) >= 12:
                raise HTTPException(429, 'Wait a minute before creating more invoices')
            q.append(now)
        snapshot = p.create(body.amount_sats)
        public_id = secrets.token_hex(24)
        db.save(public_id, snapshot)
        return view(db.get(public_id), refresh=False)

    @api.get('/api/invoices/{public_id}')
    def read(public_id: str):
        return view(db.get(public_id))

    @api.get('/api/invoices/{public_id}/qr.svg')
    def qr(public_id: str):
        result = view(db.get(public_id))
        image = qrcode.make(result['payment_uri'], image_factory=qrcode.image.svg.SvgPathImage,
                            box_size=6, border=4)
        return Response(image.to_string(), media_type='image/svg+xml')

    @api.post('/api/demo/{public_id}')
    def simulate(public_id: str, body: Simulation):
        if s.mode != 'demo':
            raise HTTPException(404, 'Demo is disabled')
        row = db.get(public_id)
        try:
            p.simulate(row['provider_id'], body.scenario)
        except KeyError:
            raise HTTPException(503, 'Demo runtime restarted') from None
        return view(row)

    @api.get('/api/export.csv')
    def export(request: Request):
        if s.mode != 'demo':
            token = request.headers.get('authorization', '')
            if not hmac.compare_digest(token, 'Bearer '+s.admin_key):
                raise HTTPException(401, 'Administrator authorization required')
        return Response(db.export(s.mode == 'demo'), media_type='text/csv; charset=utf-8',
                        headers={'Content-Disposition': 'attachment; filename="creator-rail.csv"'})

    @api.post('/api/webhook')
    async def webhook(request: Request):
        raw = await request.body()
        if len(raw) > 16_384:
            raise HTTPException(413, 'Request too large')
        if s.mode != 'btcpay':
            raise HTTPException(404, 'Webhook is disabled in demo')
        signature = 'sha256='+hmac.new(s.webhook_secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, request.headers.get('btcpay-sig', '')):
            raise HTTPException(401, 'Invalid webhook signature')
        try:
            event = json.loads(raw)
            if event['storeId'] != s.store or event['type'] not in {
                'InvoiceCreated', 'InvoiceReceivedPayment', 'InvoiceProcessing', 'InvoiceExpired',
                'InvoiceSettled', 'InvoiceInvalid', 'InvoicePaymentSettled'}:
                raise ValueError('Unsupported event')
            eid = event.get('originalDeliveryId') or event['deliveryId']
            if not isinstance(eid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', eid):
                raise ValueError('Invalid delivery identifier')
            row = db.by_provider(event['invoiceId'])
        except (ValueError, KeyError, TypeError):
            raise HTTPException(422, 'Invalid webhook event') from None
        # Refresh canonical provider state first. Payload amounts/status are never credited.
        result = await run_in_threadpool(view, row)
        first = db.delivery(eid, row['provider_id'])
        return {'processed': True, 'duplicate': not first, 'status': result['status']}

    api.mount('/static', StaticFiles(directory=ROOT/'static'), name='static')
    return api


app = create_app()
