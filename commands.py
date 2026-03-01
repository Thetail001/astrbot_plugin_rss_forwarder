from astrbot.api.event import AstrMessageEvent


class RSSCommands:
    """命令入口。"""

    scheduler = None

    async def rss_router(self, event: AstrMessageEvent):
        """兜底消息路由：在未命中 wake/at 指令条件时，仍可处理 /rss 子命令。"""
        message_text = ""
        if hasattr(event, "message_str"):
            message_text = str(getattr(event, "message_str") or "")
        elif hasattr(event, "get_message_str"):
            getter = getattr(event, "get_message_str")
            message_text = str(getter() if callable(getter) else getter or "")

        tokens = message_text.strip().split()
        if not tokens:
            return

        head = tokens[0].lstrip("/").lower()
        if head != "rss":
            return

        sub = tokens[1].lower() if len(tokens) >= 2 else ""
        route_map = {
            "list": self.rss_list,
            "status": self.rss_status,
            "run": self.rss_run,
            "pause": self.rss_pause,
            "resume": self.rss_resume,
            "reset": self.rss_reset,
        }

        handler = route_map.get(sub)
        if handler is None:
            yield event.plain_result(
                "用法：/rss [list|status|run [job_id]|pause [job_id]|resume [job_id]|reset]"
            )
            return

        async for result in handler(event):
            yield result

    async def rss_list(self, event: AstrMessageEvent):
        scheduler = self.scheduler
        config = scheduler.config
        last_results = scheduler.last_results
        paused_jobs = scheduler.paused_jobs

        lines = [
            "RSS 概览：",
            f"- feeds={len(config.feeds)} jobs={len(config.jobs)} targets={len(config.targets)}",
            f"- 运行状态：{'运行中' if scheduler.running else '未运行'}",
            f"- 暂停任务：{', '.join(sorted(paused_jobs)) if paused_jobs else '无'}",
            "",
            "任务列表：",
        ]

        for job in config.jobs:
            result = last_results.get(job.id)
            recent_success = self._format_success_time(result)
            recent_error = self._format_last_error(result)
            job_status = "已暂停" if job.id in paused_jobs else ("启用" if job.enabled else "禁用")
            lines.append(
                f"- {job.id} [{job_status}] feeds={len(job.feed_ids)} targets={len(job.target_ids)} "
                f"最近成功={recent_success} 最近错误={recent_error}"
            )

        yield event.plain_result("\n".join(lines))

    async def rss_run(self, event: AstrMessageEvent):
        job_id = self._extract_param(event)
        ok = await self.scheduler.run_job_once(job_id=job_id or None)
        if not ok:
            target = job_id or "全部任务"
            yield event.plain_result(f"手动触发失败：未找到或不可执行任务（{target}）")
            return

        if job_id:
            result = self.scheduler.last_results.get(job_id)
            yield event.plain_result(
                f"已触发任务 {job_id}。最近成功={self._format_success_time(result)} "
                f"最近错误={self._format_last_error(result)}"
            )
            return

        yield event.plain_result("已触发全部启用且未暂停任务。")

    async def rss_reset(self, event: AstrMessageEvent):
        """清空已推送去重记录，便于调试或重新全量推送。"""
        scheduler = self.scheduler
        deleted = await scheduler.storage.clear_seen()
        yield event.plain_result(f"已清空去重记录：{deleted} 条。")

    async def rss_status(self, event: AstrMessageEvent):
        scheduler = self.scheduler
        config = scheduler.config
        last_results = scheduler.last_results

        success_times = [
            result.started_at for result in last_results.values() if not result.error_summary
        ]
        recent_success = (
            max(success_times).strftime("%Y-%m-%d %H:%M:%S") if success_times else "暂无"
        )

        errors = [result.error_summary for result in last_results.values() if result.error_summary]
        recent_error = errors[-1] if errors else "无"

        lines = [
            "RSS 状态：",
            f"- 调度器：{'运行中' if scheduler.running else '未运行'}",
            f"- feeds={len(config.feeds)} jobs={len(config.jobs)} targets={len(config.targets)}",
            f"- 最近成功：{recent_success}",
            f"- 最近错误：{recent_error}",
        ]
        yield event.plain_result("\n".join(lines))

    async def rss_pause(self, event: AstrMessageEvent):
        job_id = self._extract_param(event)
        if not job_id:
            yield event.plain_result("用法：/rss pause [job_id]")
            return

        ok = await self.scheduler.pause_job(job_id)
        if not ok:
            yield event.plain_result(f"暂停失败：任务不存在或未启用（{job_id}）")
            return

        result = self.scheduler.last_results.get(job_id)
        yield event.plain_result(
            f"任务已暂停：{job_id}。最近成功={self._format_success_time(result)} 最近错误={self._format_last_error(result)}"
        )

    async def rss_resume(self, event: AstrMessageEvent):
        job_id = self._extract_param(event)
        if not job_id:
            yield event.plain_result("用法：/rss resume [job_id]")
            return

        ok = self.scheduler.resume_job(job_id)
        if not ok:
            yield event.plain_result(f"恢复失败：任务不存在或未启用（{job_id}）")
            return

        result = self.scheduler.last_results.get(job_id)
        yield event.plain_result(
            f"任务已恢复：{job_id}。最近成功={self._format_success_time(result)} 最近错误={self._format_last_error(result)}"
        )

    @staticmethod
    def _extract_param(event: AstrMessageEvent) -> str:
        message_text = ""
        if hasattr(event, "message_str"):
            message_text = str(getattr(event, "message_str") or "")
        elif hasattr(event, "get_message_str"):
            getter = getattr(event, "get_message_str")
            message_text = str(getter() if callable(getter) else getter or "")

        tokens = message_text.strip().split()
        return tokens[2].strip() if len(tokens) >= 3 else ""

    @staticmethod
    def _format_success_time(result) -> str:
        if result is None or result.error_summary:
            return "暂无"
        return result.started_at.strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _format_last_error(result) -> str:
        if result is None or not result.error_summary:
            return "无"
        return result.error_summary
