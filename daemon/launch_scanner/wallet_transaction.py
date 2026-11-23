"""Pure successful wallet balance evidence, shared by actual acquisition paths.

Not swap instruction attribution, exact raw-decimal or loaded-address validation.
Unknown/invalid returns None, valid no-delta returns measured empty maps.
"""
import math


def wallet_balance_evidence(wallet,sig,tx):
    try:
        # Failed chain transactions are not buys. Missing execution
        # status is unknown, not success. Validate wallet evidence
        # before returning any partial wallet-sighting evidence.
        if not isinstance(tx, dict) or not isinstance(tx.get("meta"), dict):
            return None
        meta = tx["meta"]
        if "err" not in meta or meta["err"] is not None:
            return None
        keys = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys")
        if not isinstance(keys, list):
            return None
        signatures = (tx.get("transaction") or {}).get("signatures")
        if (not isinstance(signatures, list) or not signatures
                or signatures[0] != sig):
            return None
        keys=[item.get('pubkey') if isinstance(item,dict) else item for item in keys]
        if wallet not in keys:return None
        idx = keys.index(wallet)
        pre_bal, post_bal = meta.get("preBalances"), meta.get("postBalances")
        def number(value):
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value < 0):
                raise ValueError("invalid measured amount")
            return float(value)
        if not isinstance(pre_bal, list) or not isinstance(post_bal, list):
            return None
        sol_out = max(0.0, (number(pre_bal[idx]) - number(post_bal[idx])) / 1e9)
        block_ts = number(tx.get("blockTime"))
        if block_ts <= 0:
            return None
        before, after = {}, {}
        account_identities = {}
        for field, target in (("preTokenBalances", before), ("postTokenBalances", after)):
            balances = meta.get(field)
            if not isinstance(balances, list):
                return None
            seen_indices = set()
            for balance in balances:
                if not isinstance(balance, dict):
                    return None
                account_index = balance.get("accountIndex")
                if (type(account_index) is not int or account_index < 0
                        or account_index >= len(keys) or account_index in seen_indices):
                    return None
                seen_indices.add(account_index)
                # Missing owner cannot establish that a pre-holding
                # was somebody else's. Refuse ambiguous deltas.
                if not isinstance(balance.get("owner"), str) or not balance["owner"]:
                    return None
                mint = balance.get("mint")
                identity = (balance["owner"], mint)
                if account_index in account_identities and account_identities[account_index] != identity:
                    return None
                account_identities[account_index] = identity
                if balance["owner"] != wallet:
                    continue
                if not isinstance(mint, str) or not mint:
                    return None
                amount = number((balance.get("uiTokenAmount") or {}).get("uiAmount"))
                # Multiple accounts of the same mint are summed;
                # an account-to-account transfer must not become a buy.
                target[mint] = number(target.get(mint, 0.0) + amount)
        return {'before':before,'after':after,'sol_out':sol_out,'block_ts':block_ts}
    except (ValueError, TypeError, IndexError, AttributeError, OverflowError):
        return None
