import keyring

SERVICE = "mariadb-table-snapshot"


def password(profile, ephemeral=None):
    if ephemeral and profile["id"] in ephemeral:
        return ephemeral[profile["id"]]
    if profile.get("secret_ref"):
        value = keyring.get_password(SERVICE, profile["secret_ref"])
        if value is not None:
            return value
    raise ValueError(f"연결 {profile['name']}: 비밀번호를 입력하세요.")


def save_secret(profile_id, value):
    backend = keyring.get_keyring()
    module = type(backend).__module__
    if not module.startswith(
        ("keyring.backends.macOS", "keyring.backends.Windows", "keyring.backends.SecretService")
    ):
        raise ValueError("안전한 OS 자격 증명 저장소가 없습니다. 저장 없이 입력하세요.")
    keyring.set_password(SERVICE, profile_id, value)


def delete_secret(profile_id):
    try:
        keyring.delete_password(SERVICE, profile_id)
    except keyring.errors.PasswordDeleteError:
        pass


def safe_error(exc, secrets=()):
    # Database exceptions may embed SQL values. Keep error class/code, suppress server text.
    if type(exc).__module__.startswith("pymysql"):
        code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else "unknown"
        hints = {
            1045: "인증/접속 권한 확인",
            1049: "DB명 확인",
            1142: "필요 권한 부족",
            2003: "호스트/포트/네트워크 확인",
            2013: "통신 중 연결 끊김",
            2006: "연결 종료",
            1406: "컬럼 길이 초과",
            1062: "키 중복",
            1118: "행/인덱스 크기 초과",
        }
        return (
            f"{type(exc).__name__} [{code}]: {hints.get(code, 'DB 오류; 서버 로그를 관리자와 확인하세요.')}"
        )
    text = f"{type(exc).__name__}: {exc}"
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text[:1500]
