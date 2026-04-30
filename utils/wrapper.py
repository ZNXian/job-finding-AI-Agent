from functools import wraps
import traceback
from config import log
from fastapi import HTTPException

def handle_api_exception(func):
    """简单的API异常处理装饰器"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except HTTPException:
            # 透传 FastAPI 的标准异常（保持真实 HTTP 状态码）
            raise
        except Exception as e:
            log.error(f"接口异常: {str(e)}\n{traceback.format_exc()}")
            return {
                "code": 500,
                "status": "error",
                "msg": f"处理失败: {str(e)}"
            }
    return wrapper

# 异步版本
def handle_api_exception_async(func):
    """异步API异常处理装饰器"""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except HTTPException:
            # 透传 FastAPI 的标准异常（保持真实 HTTP 状态码）
            raise
        except Exception as e:
            log.error(f"接口异常: {str(e)}\n{traceback.format_exc()}")
            return {
                "code": 500,
                "status": "error",
                "msg": f"处理失败: {str(e)}"
            }
    return wrapper