from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def validate_password(password):
    if len(password.encode("utf-8")) > 72:
        return "password_too_long"
    if len(password) < 12:
        return "password_too_short"
    return None
