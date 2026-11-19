import asyncio

from astrbot.api import logger


SHUTDOWN_TIMEOUT = 3.0


def consume_task(task):
    if not task.cancelled():
        task.exception()


async def finish_tasks(tasks):
    tasks = set(tasks) - {asyncio.current_task()}
    if not tasks:
        return
    done, pending = await asyncio.wait(tasks, timeout=SHUTDOWN_TIMEOUT)
    for task in done:
        consume_task(task)
    for task in pending:
        task.cancel()
        task.add_done_callback(consume_task)
    if pending:
        logger.warning('[Jupiter] %s 个后台任务未在关闭时限内退出，已隔离旧实例', len(pending))


