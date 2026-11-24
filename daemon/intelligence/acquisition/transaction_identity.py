"""Identity consistency for jsonParsed named-transaction recovery reads.

No signature authentication, execution, slot, time or decoder completeness claim.
"""


def response_signature_matches(tx, requested):
    if not isinstance(requested, str) or not requested or not isinstance(tx, dict):
        return False
    transaction = tx.get('transaction')
    if not isinstance(transaction, dict):
        return False
    signatures = transaction.get('signatures')
    return (isinstance(signatures, list) and bool(signatures)
            and type(signatures[0]) is str and signatures[0] == requested)
