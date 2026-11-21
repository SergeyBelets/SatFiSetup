"""Typed owner actions using the existing EOA nonce/cost/recovery lane."""
from web3 import Web3
from .bootstrap import artifact
from .btc_fee_reserve import read,number,ZERO
from .recipient_profiles import registry_policy,review,context_hash


def action(client,args):
    b,pool,ctx,_,_=client.context();policy=registry_policy(client.w3,ctx,b.number)
    if args.action=='wallet-create':
        owner=args.wallet_owner or client.account.address
        if not Web3.is_checksum_address(owner) or owner==ZERO or client.w3.eth.get_code(owner,block_identifier=b.number):
            raise ValueError('RECIPIENT_WALLET_PROFILE')
        # An explicit seed makes an interrupted CREATE2 request reproducible.
        if not args.wallet_salt:raise ValueError('RECIPIENT_WALLET_SALT_REQUIRED')
        salt=bytes.fromhex(args.wallet_salt.removeprefix('0x'))
        if len(salt)!=32:raise ValueError('RECIPIENT_WALLET_PROFILE')
        from .contract_provenance import creation_input
        from eth_abi import encode
        from eth_utils import keccak
        expected=Web3.to_checksum_address(keccak(b'\xff'+bytes.fromhex(policy.address[2:])+keccak(encode(['address','bytes32'],[owner,salt]))+
            keccak(creation_input('RecipientWalletV1',[owner])))[-20:])
        if read(policy,b.number,'predictWallet',owner,salt)!=expected:raise ValueError('RECIPIENT_POLICY_ORIGIN')
        client.send('recipient-wallet-create-'+expected,'createWallet',[owner,'0x'+salt.hex()],target=policy.address,kind='RecipientPolicy',receipt=True)
        return dict(status='RECIPIENT_WALLET_CREATED',wallet=expected,owner=owner,policy=policy.address,ready=False)
    wallet=args.wallet
    if not Web3.is_checksum_address(wallet):raise ValueError('RECIPIENT_WALLET_PROFILE')
    from eth_utils import keccak
    choice=[1,keccak(bytes(client.w3.eth.get_code(wallet,block_identifier=b.number))),client.account.address,b'x'*32,bytes(32)]
    context=context_hash(client.w3.eth.chain_id,policy.address,ctx['registry'].address,pool.address,
        client.read(pool,b,'mpToken'),client.account.address,wallet)
    review(client.w3,policy,wallet,choice,context,b.number)
    token=client.w3.eth.contract(address=client.read(pool,b,'mpToken'),abi=artifact('PoolMpToken')['abi'])
    amount=number(args.amount_units,1,2**256-1)
    if args.action=='wallet-transfer':
        if not Web3.is_checksum_address(args.recipient) or args.recipient==ZERO:raise ValueError('RECIPIENT_PROFILE')
        target=token.address;data=token.functions.transfer(args.recipient,amount)._encode_transaction_data()
    elif args.action=='wallet-approve':
        if args.spender!=client.read(pool,b,'escrow'):raise ValueError('RECIPIENT_WALLET_SPENDER')
        target=token.address;data=token.functions.approve(args.spender,amount)._encode_transaction_data()
    else:
        from . import crypto
        script=crypto.call('bitcoin_address_script_v1',address=args.bitcoin_destination,network=client.c['bitcoin_network'])['script']
        escrow=client.w3.eth.contract(address=client.read(pool,b,'escrow'),abi=artifact('BitcoinWithdrawalEscrow')['abi'])
        identifier=number(args.withdrawal_id,0,2**256-1)
        target=escrow.address;data=escrow.functions.requestDirect(identifier,token.address,amount,bytes.fromhex(script))._encode_transaction_data()
    # Duplicate owner requests reuse exact bytes. A different withdrawal ID or
    # transfer operation salt is required for another otherwise identical action.
    if not args.wallet_salt:raise ValueError('RECIPIENT_WALLET_SALT_REQUIRED')
    from .btc_fee_reserve import digest
    key=digest([policy.address,wallet,args.action,target,data,args.wallet_salt])
    client.send('recipient-wallet-'+key,'execute',[target,'0',data],target=wallet,kind='RecipientWalletV1',receipt=True)
    return dict(status='RECIPIENT_WALLET_ACTION_CONFIRMED',wallet=wallet,owner=client.account.address,
        action=args.action,token=token.address,amount_units=str(amount),ready=False)
