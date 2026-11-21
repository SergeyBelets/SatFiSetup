"""Untrusted local mailbox: immutable authenticated public packages and HPKE ciphertext only."""
import json
import sqlite3
from eth_account import Account
from eth_account.messages import encode_typed_data
from .dkg_protocol import typed_message

MAX_MESSAGE = 131072


def sign(manifest, kind, private_key, **fields):
    m = typed_message(manifest, kind, **fields)
    return Account.sign_message(encode_typed_data(m['domain'], m['types'], m['message']), private_key).signature.hex()


def recover(manifest, kind, signature, **fields):
    m = typed_message(manifest, kind, **fields)
    return Account.recover_message(encode_typed_data(m['domain'],m['types'],m['message']),signature=signature)


class Mailbox:
    def __init__(self, path):
        self.path = str(path)
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS messages(context TEXT, kind TEXT, sender TEXT, recipient TEXT, body TEXT NOT NULL, PRIMARY KEY(context,kind,sender,recipient))')

    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute('PRAGMA synchronous=FULL')
        return db

    def put(self, manifest, kind, sender, recipient, body):
        self.put_many(manifest, [(kind, sender, recipient, body)])

    def put_many(self, manifest, messages):
        if not messages or len(messages) > 16:
            raise ValueError('PUBLIC_PACKET_SCHEMA')
        values = [self._validated(manifest, *message) for message in messages]
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for context, kind, sender, recipient, encoded in values:
                row = db.execute('SELECT body FROM messages WHERE context=? AND kind=? AND sender=? AND recipient=?',
                                 (context,kind,sender,recipient)).fetchone()
                if row and row[0] != encoded:
                    raise ValueError('CONFLICTING_RETRY')
                db.execute('INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?)',
                           (context,kind,sender,recipient,encoded))

    @staticmethod
    def _validated(manifest, kind, sender, recipient, body):
        encoded = json.dumps(body, sort_keys=True, separators=(',',':'))
        if len(encoded.encode()) > MAX_MESSAGE:
            raise ValueError('MESSAGE_TOO_LARGE')
        context = manifest.context.hex()
        if kind == 'key':
            who = recover(manifest, 'DKGTransportKey', body['signature'], participant=sender,
                          transportPublicKey=manifest.transport_keys[manifest.index(sender)-1])
        elif kind == 'envelope':
            who = recover(manifest, 'DKGEnvelope', body['signature'],sender=sender,recipient=recipient,
                          shareHash=bytes.fromhex(body['hash']))
        elif kind == 'round1':
            # Authenticity is supplied by the sender's immutable on-chain hash, checked by every recipient.
            who = sender
        else:
            raise ValueError('INVALID_MESSAGE_KIND')
        if who.lower() != sender.lower():
            raise ValueError('SENDER_MISMATCH')
        return context, kind, sender.lower(), recipient.lower(), encoded

    def get(self, manifest, kind, sender, recipient=''):
        with self.db() as db:
            row = db.execute('SELECT body FROM messages WHERE context=? AND kind=? AND sender=? AND recipient=?',
                             (manifest.context.hex(),kind,sender.lower(),recipient.lower())).fetchone()
        return json.loads(row[0]) if row else None
