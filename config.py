from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # FreeSWITCH ESL
    FS_HOST: str = "127.0.0.1"
    FS_PORT: int = 8021
    FS_PASSWORD: str = "ClueCon"

    # SIP / Gateway
    SIP_GATEWAY: str = "mygateway"
    CALLER_ID_NUMBER: str = "1234567890"
    CALLER_ID_NAME: str = "Dialer"
    DIAL_PREFIX: str = ""             # prefix sent to carrier before the number e.g. "4164#"

    # Anthropic
    ANTHROPIC_API_KEY: str = ""
    CLAUDE_MODEL: str = "claude-sonnet-4-6"

    # Dialer behaviour
    MAX_CONCURRENT_CALLS: int = 10
    DIAL_TIMEOUT: int = 30          # seconds to wait for answer
    WRAP_UP_TIME: int = 30          # seconds after call before agent is idle
    DROP_RATE_LIMIT: float = 0.03   # 3 % max allowed drop rate
    PACING_INTERVAL: float = 5.0    # seconds between pacing cycles
    AMD_ENABLED: bool = True

    # Call recording
    RECORDING_ENABLED: bool = True
    RECORDING_DIR: str = "/var/lib/freeswitch/recordings"
    RECORDING_FORMAT: str = "wav"

    # Web server
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
