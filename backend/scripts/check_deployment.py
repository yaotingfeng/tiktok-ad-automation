"""离线验证容器运行配置；不连接数据库、Redis 或任何外部平台。"""

import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def check() -> None:
    from app.core.config import settings
    from app.integrations.tiktok.mcp.protocol import load_mcp_protocol
    from app.integrations.tiktok.mcp_auth.service import load_registration
    from app.jobs.admission import admission_policy

    settings.require_connection_encryption()
    settings.require_object_storage()
    load_registration(load_mcp_protocol())
    # 复用应用实际校验器，避免部署脚本维护第二套额度字段定义。
    admission_policy("deployment.check")
    for endpoint in ("materials.upload_video_file", "materials.upload_video_url"):
        if admission_policy(endpoint).lease_ms <= 905_000:
            raise ValueError(
                "upload lease must exceed the 900 second hard limit plus cleanup margin"
            )
    logger.info(
        "PASS: runtime settings, encryption, object storage configuration, MCP registration and call policies"
    )
    logger.info(
        "Offline configuration check only; external connectivity and OAuth are not verified."
    )


if __name__ == "__main__":
    try:
        check()
    except Exception as error:
        # 校验异常可能含输入配置；输出类型即可，严禁回显密钥或令牌。
        logger.error("Deployment configuration failed (%s)", type(error).__name__)
        sys.exit(2)
