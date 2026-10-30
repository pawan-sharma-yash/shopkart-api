import secrets
import string


def generate_public_order_id() -> str:
    """Return a random 10-character uppercase alphanumeric order identifier."""
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(10))
