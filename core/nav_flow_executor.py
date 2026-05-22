"""
根据 data/nav_flow_to_merchant.json 执行键盘导航（标题 → 小壶商人商店 UI）。
"""

from __future__ import annotations

import json
import time
from typing import Callable, List, Optional, Tuple

import pydirectinput

from core.utils import get_resource_path, log_debug


LogFn = Callable[[str, str], None]
IsRunningFn = Callable[[], bool]


class NavFlowExecutor:
    """读取 JSON 配置，OCR 判态 + 键盘操作。"""

    def __init__(
        self,
        ocr_engine,
        repo_filter,
        is_running: IsRunningFn,
        config_path: Optional[str] = None,
    ):
        self.ocr_engine = ocr_engine
        self.repo_filter = repo_filter
        self.is_running = is_running
        path = config_path or get_resource_path("data/nav_flow_to_merchant.json")
        with open(path, "r", encoding="utf-8") as f:
            self.config = json.load(f)
        self.keys = self.config.get("keys", {})
        ocr_cfg = self.config.get("ocr", {})
        self.poll_interval_s = ocr_cfg.get("poll_interval_ms", 400) / 1000.0
        self.step_timeout_s = ocr_cfg.get("step_timeout_ms", 60000) / 1000.0
        self.optional_timeout_s = ocr_cfg.get(
            "optional_step_timeout_ms", 8000
        ) / 1000.0
        self._step_index_by_id = {
            s["id"]: i for i, s in enumerate(self.config.get("steps", []))
        }
        self._action_timing = {}
        self._step_defaults = {}
        self._flow_step_index_by_id: dict = {}
        self._pending_jump_index: Optional[int] = None

    def run(self, log: LogFn) -> bool:
        """正向：标题 → 小壶商人商店。"""
        return self._run_steps(
            self.config.get("steps", []),
            self.config.get("start_state_detection"),
            log,
            flow_title="完全自动化导航（标题 → 小壶商人）",
            handoff_message="已进入商店界面，交接购买流程",
        )

    def run_reverse(self, log: LogFn) -> bool:
        """逆向：购买结束后退出到标题（SL 用）。"""
        section = self.config.get("reverse_flow_sl", {})
        self._action_timing = section.get("action_timing", {})
        self._step_defaults = section.get("step_defaults", {})
        try:
            return self._run_steps(
                section.get("steps", []),
                section.get("start_state_detection"),
                log,
                flow_title="SL 退出到标题（ESC×2→F1→up×1→结束游戏→F×2→←→F）",
                handoff_message=None,
            )
        finally:
            self._action_timing = {}
            self._step_defaults = {}

    def _run_steps(
        self,
        steps: list,
        start_detection: Optional[dict],
        log: LogFn,
        flow_title: str,
        handoff_message: Optional[str],
    ) -> bool:
        if not steps:
            log("导航配置无步骤", "ERROR")
            return False

        self.repo_filter.refresh_window_info()
        if not self.repo_filter.game_window:
            log(
                "未找到游戏窗口（标题需含 NIGHTREIGN），请先启动游戏",
                "ERROR",
            )
            return False

        log(f"开始{flow_title}...", "INFO")
        self._flow_step_index_by_id = {
            s["id"]: i for i, s in enumerate(steps)
        }
        start_index, state_label = self._resolve_start_index(
            steps, log, start_detection, self._flow_step_index_by_id
        )
        if start_index > 0:
            log(
                f"当前状态: {state_label} → 从步骤 {start_index + 1}/"
                f"{len(steps)}「{steps[start_index].get('name', '')}」继续",
                "INFO",
            )
        else:
            log(f"当前状态: {state_label} → 从第 1 步顺序执行", "INFO")

        index = start_index
        while index < len(steps):
            step = steps[index]
            if not self.is_running():
                log("导航已取消", "WARNING")
                return False
            self._pending_jump_index = None
            if not self._run_step(step, index, len(steps), log):
                if self._pending_jump_index is not None:
                    index = self._pending_jump_index
                    log(
                        f"[导航] 状态恢复后从步骤 {index + 1}/"
                        f"{len(steps)} 继续",
                        "INFO",
                    )
                    continue
                log(
                    f"导航失败: {step.get('name', step.get('id'))}",
                    "ERROR",
                )
                return False
            if self._pending_jump_index is not None:
                index = self._pending_jump_index
                log(
                    f"[导航] 状态恢复后从步骤 {index + 1}/{len(steps)} 继续",
                    "INFO",
                )
                continue
            if handoff_message and step.get("handoff"):
                log(handoff_message, "SUCCESS")
                return True
            index += 1

        log("导航步骤已走完", "SUCCESS")
        return True

    def _resolve_start_index(
        self,
        steps: list,
        log: LogFn,
        start_detection: Optional[dict],
        step_index_by_id: dict,
    ) -> tuple[int, str]:
        """用 start_state_detection 判断当前画面，避免误判。"""
        texts = self._read_screen_texts()
        if not texts:
            log(
                "OCR 未读到文字：请把游戏切到前台（勿遮住窗口）",
                "WARNING",
            )
            return 0, "未知（无 OCR）"

        preview = " | ".join(texts[:8])
        if len(texts) > 8:
            preview += " ..."
        log(f"当前画面识别: {preview}", "INFO")

        priority = (start_detection or {}).get("priority", [])
        if priority:
            for entry in priority:
                if self._state_entry_matches(entry, texts):
                    step_id = entry.get("step_id", "")
                    index = step_index_by_id.get(step_id, 0)
                    label = entry.get("label", step_id)
                    return index, label
            recovered = self._recover_unknown_screen(log)
            if recovered is not None:
                return recovered, "状态恢复"
            return 0, "未匹配任何状态（从第 1 步试）"

        for index in range(len(steps) - 1, -1, -1):
            step = steps[index]
            if self._step_detect_matches(step, texts):
                return index, step.get("name", step.get("id", ""))

        recovered = self._recover_unknown_screen(log)
        if recovered is not None:
            return recovered, "状态恢复"
        return 0, "未匹配（从第 1 步试）"

    def _global_known_state_entries(self) -> List[dict]:
        return self.config.get("global_known_states", {}).get("priority", [])

    def _scan_known_state(
        self, texts: List[str], log: LogFn
    ) -> Optional[Tuple[str, str]]:
        """在 global_known_states 中匹配当前界面，返回 (step_id, label)。"""
        for entry in self._global_known_state_entries():
            if self._state_entry_matches(entry, texts):
                return entry.get("step_id", ""), entry.get("label", "")
        return None

    def _recover_unknown_screen(self, log: LogFn) -> Optional[int]:
        """
        界面未知时：全量状态匹配 → 等待 → 再匹配 → 按 M 开地图。
        返回当前流程中应继续的步骤下标。
        """
        cfg = self.config.get("unknown_state_recovery", {})
        wait_s = cfg.get("wait_unknown_ms", 3000) / 1000.0
        step_index_by_id = self._flow_step_index_by_id

        def _log_ocr(texts: List[str], prefix: str) -> None:
            preview = " | ".join(texts[:10]) if texts else "(无)"
            if len(preview) > 120:
                preview = preview[:117] + "..."
            log(f"{prefix} OCR: {preview}", "INFO")

        def _resolve_hit(
            hit: Optional[Tuple[str, str]], phase: str
        ) -> Optional[int]:
            if not hit:
                return None
            step_id, label = hit
            idx = step_index_by_id.get(step_id)
            if idx is not None:
                log(
                    f"[导航] 状态识别({phase}): 「{label}」→ 步骤 {idx + 1}",
                    "INFO",
                )
                return idx
            log(
                f"[导航] 识别到「{label}」但不在当前导航流程中",
                "WARNING",
            )
            return None

        texts = self._read_screen_texts()
        _log_ocr(texts, "[导航] 扫描已知界面")
        idx = _resolve_hit(self._scan_known_state(texts, log), "扫描")
        if idx is not None:
            return idx

        log("[导航] 未匹配任何已知界面，等待 3 秒后再识别…", "WARNING")
        deadline = time.time() + wait_s
        while time.time() < deadline:
            if not self.is_running():
                return None
            time.sleep(self.poll_interval_s)
            texts = self._read_screen_texts()
            idx = _resolve_hit(self._scan_known_state(texts, log), "等待后")
            if idx is not None:
                return idx

        texts = self._read_screen_texts()
        _log_ocr(texts, "[导航] 仍未知，按 M 前")
        log("[导航] 仍无法识别界面，尝试按 M 打开地图…", "INFO")
        map_key = self._resolve_key(cfg.get("map_key_ref", "open_map_menu"))
        pydirectinput.press(map_key)
        timing = getattr(self, "_action_timing", {}) or {}
        after = timing.get("delay_after_ms", 200) / 1000.0
        time.sleep(max(after, 0.5))

        texts = self._read_screen_texts()
        _log_ocr(texts, "[导航] 按 M 后")
        idx = _resolve_hit(self._scan_known_state(texts, log), "按M后")
        if idx is not None:
            return idx

        map_rule = cfg.get("map_open_detect")
        if map_rule and self._match_rule(map_rule, texts):
            mid = cfg.get("map_recovery_step_id", "round_table_select_merchant")
            if mid in step_index_by_id:
                log("[导航] M 已打开圆桌地图，从选商人步骤继续", "INFO")
                return step_index_by_id[mid]

        log("[导航] 状态恢复失败：仍无法判断当前界面", "ERROR")
        return None

    def _state_entry_matches(self, entry: dict, texts: List[str]) -> bool:
        match_rule = entry.get("match")
        if not match_rule or not self._match_rule(match_rule, texts):
            return False
        must_not = entry.get("must_not")
        if must_not and self._match_rule(must_not, texts):
            return False
        return True

    def _step_detect_matches(self, step: dict, texts: List[str]) -> bool:
        detect = step.get("detect")
        if detect and self._match_rule(detect, texts):
            return True
        detect_alt = step.get("detect_alt")
        if detect_alt and self._match_rule(detect_alt, texts):
            return True
        return False

    def _max_step_retries(self, step: dict) -> int:
        defaults = getattr(self, "_step_defaults", {}) or {}
        raw = step.get("max_retries", defaults.get("max_retries", 3))
        return max(1, int(raw))

    def _run_step(
        self, step: dict, index: int, total: int, log: LogFn
    ) -> bool:
        step_id = step.get("id", "")
        name = step.get("name", step_id)
        optional = step.get("optional", False)
        max_retries = self._max_step_retries(step)
        log(f"[导航 {index + 1}/{total}] {name}", "INFO")

        if step.get("skip_actions_if_detect_alt"):
            texts = self._read_screen_texts()
            detect_alt = step.get("detect_alt")
            if detect_alt and self._match_rule(detect_alt, texts):
                log(f"[导航] 已处于目标界面，跳过按键: {name}", "INFO")
                success = step.get("success_detect")
                if success and self._wait_rule(
                    success,
                    None,
                    15.0,
                    True,
                    log,
                    silent=True,
                    wait_hint="步骤完成",
                ):
                    return True

        if step.get("menu_navigate") and max_retries > 0:
            self._ensure_map_open(step, log)

        timeout = self.optional_timeout_s if optional else self.step_timeout_s
        success_rule = step.get("success_detect")

        for attempt in range(1, max_retries + 1):
            if attempt > 1:
                log(
                    f"[导航] 未确认步骤完成，第 {attempt}/{max_retries} 次重试: "
                    f"{name}",
                    "WARNING",
                )

            if not self._wait_rule(
                step.get("detect"),
                step.get("detect_alt"),
                timeout,
                optional,
                log,
                wait_hint=name,
                step=step,
            ):
                if optional:
                    log(f"[导航] 跳过可选步骤: {name}", "INFO")
                    return True
                jump = self._recover_unknown_screen(log)
                if jump is not None:
                    self._pending_jump_index = jump
                    return False
                if attempt >= max_retries:
                    return False
                continue

            if step.get("menu_position_navigate"):
                if not self._ensure_menu_position(step, log):
                    jump = self._recover_unknown_screen(log)
                    if jump is not None:
                        self._pending_jump_index = jump
                        return False
                    if attempt >= max_retries:
                        return False
                    continue

            self._execute_actions(step.get("actions", []), log, step=step)

            if not success_rule:
                return True

            ok = self._wait_rule(
                success_rule,
                None,
                20.0,
                False,
                log,
                silent=(attempt == 1),
                wait_hint="步骤完成",
            )
            if ok:
                if attempt > 1:
                    log(f"[导航] 步骤完成（第 {attempt} 次尝试）", "INFO")
                return True

            jump = self._recover_unknown_screen(log)
            if jump is not None:
                self._pending_jump_index = jump
                return False

        log(
            f"[导航] 步骤失败（已重试 {max_retries} 次仍未确认完成）: {name}",
            "ERROR",
        )
        return False if not optional else True

    def _ensure_menu_position(self, step: dict, log: LogFn) -> bool:
        """按配置 down/up 后 OCR 确认「结束游戏」等锚点，再执行后续 F。"""
        cfg = step.get("menu_position_navigate", {})
        down_n = int(cfg.get("down_count", 0))
        up_n = int(cfg.get("up_count", 1))
        position = cfg.get("position_detect")
        if not position:
            return True

        label = "、".join(position.get("texts", ["目标项"]))
        parts = []
        if down_n > 0:
            parts.append(f"down×{down_n}")
        if up_n > 0:
            parts.append(f"up×{up_n}")
        move_desc = " → ".join(parts) if parts else "无按键"
        log(
            f"[导航] 菜单定位: {move_desc}，等待 OCR 出现「{label}」",
            "INFO",
        )
        initial_actions = []
        if down_n > 0:
            initial_actions.append(
                {"type": "key", "key_ref": "menu_down", "repeat": down_n}
            )
        if up_n > 0:
            initial_actions.append(
                {"type": "key", "key_ref": "menu_up", "repeat": up_n}
            )
        if initial_actions:
            self._execute_actions(initial_actions, log, step=step)

        timeout = float(cfg.get("position_timeout_s", 20.0))
        if self._wait_rule(
            position,
            None,
            timeout,
            False,
            log,
            wait_hint=f"菜单项「{label}」",
        ):
            log(f"[导航] 已识别「{label}」，菜单位置正确", "INFO")
            return True

        fine = cfg.get("fine_tune", {})
        max_extra = int(fine.get("max_retries", 8))
        extra_down = int(fine.get("down_per_retry", 1))
        for extra in range(max_extra):
            log(
                f"[导航] 未看到「{label}」，补按 down×{extra_down} "
                f"({extra + 1}/{max_extra})",
                "WARNING",
            )
            self._execute_actions(
                [{"type": "key", "key_ref": "menu_down", "repeat": extra_down}],
                log,
                step=step,
            )
            if self._wait_rule(
                position,
                None,
                4.0,
                False,
                log,
                silent=True,
                wait_hint=f"菜单项「{label}」",
            ):
                log(f"[导航] 补按后已识别「{label}」", "INFO")
                return True

        log(f"[导航] 无法确认菜单停在「{label}」", "ERROR")
        return False

    def _ensure_map_open(self, step: dict, log: LogFn) -> None:
        """进游戏后需先按 M 才看得到「圆桌厅堂/小壶商人」，须在等待 detect 之前调用。"""
        menu = step.get("menu_navigate", {})
        open_cfg = menu.get("open_map_if_needed", {})
        if not open_cfg.get("enabled"):
            return

        texts = self._read_screen_texts()
        if self._is_map_menu_visible(step, texts):
            log("[导航] 地图菜单已打开，无需按 M", "INFO")
            return

        action = open_cfg.get("action", {})
        if not action:
            action = {
                "type": "key",
                "key_ref": "open_map_menu",
                "repeat": 1,
                "delay_after_ms": 800,
            }
        log("[导航] 未检测到地图菜单，按 M 打开圆桌地图", "INFO")
        self._execute_actions([action], log)
        time.sleep(0.5)

    def _is_map_menu_visible(self, step: dict, texts: List[str]) -> bool:
        detect = step.get("detect")
        if detect and self._match_rule(detect, texts):
            return True
        menu = step.get("menu_navigate", {})
        hints = menu.get("open_map_if_needed", {}).get("detect_missing_texts", [])
        if hints and self._texts_match(hints, texts, "any"):
            return True
        return False

    def _execute_actions(
        self, actions: list, log: LogFn, step: Optional[dict] = None
    ) -> None:
        menu_down_override = None
        if step:
            count = step.get("menu_navigate", {}).get("menu_down_count")
            if count is not None:
                menu_down_override = int(count)

        for action in actions:
            if not self.is_running():
                return
            action_type = action.get("type")
            if action_type == "wait":
                ms = action.get("ms", 500)
                time.sleep(ms / 1000.0)
            elif action_type == "key":
                key_ref = action.get("key_ref", "confirm")
                key = self._resolve_key(key_ref)
                repeat = int(action.get("repeat", 1))
                if key_ref == "menu_down" and menu_down_override is not None:
                    repeat = menu_down_override
                timing = getattr(self, "_action_timing", {}) or {}
                delay_between = (
                    action.get("delay_between_ms", timing.get("delay_between_ms", 0))
                    / 1000.0
                )
                delay_after = (
                    action.get("delay_after_ms", timing.get("delay_after_ms", 0))
                    / 1000.0
                )
                log(f"[导航] 按键 {key} x{repeat}", "INFO")
                for i in range(repeat):
                    if not self.is_running():
                        return
                    pydirectinput.press(key)
                    if i < repeat - 1 and delay_between > 0:
                        time.sleep(delay_between)
                if delay_after > 0:
                    time.sleep(delay_after)

    def _resolve_key(self, key_ref: str) -> str:
        if key_ref in self.keys and not key_ref.startswith("_"):
            return str(self.keys[key_ref])
        return key_ref

    def _wait_rule(
        self,
        detect: Optional[dict],
        detect_alt: Optional[dict],
        timeout: float,
        optional: bool,
        log: LogFn,
        silent: bool = False,
        wait_hint: str = "",
        step: Optional[dict] = None,
    ) -> bool:
        if not detect and not detect_alt:
            return True
        deadline = time.time() + timeout
        wait_start = time.time()
        last_progress_log = time.time()
        map_retry_done = False
        shop_esc_retry_done = False
        while time.time() < deadline:
            if not self.is_running():
                return False
            texts = self._read_screen_texts()
            region_texts = self._read_region_texts(detect)
            if region_texts:
                texts = texts + region_texts
            if detect and self._match_rule(detect, texts):
                return True
            if detect_alt and self._match_rule(detect_alt, texts):
                return True
            if (
                step
                and step.get("menu_navigate")
                and not map_retry_done
                and time.time() - wait_start > 8.0
            ):
                log("[导航] 仍未看到地图菜单，再次按 M", "INFO")
                self._ensure_map_open(step, log)
                map_retry_done = True
            if (
                step
                and step.get("retry_esc_if_shop_visible")
                and not shop_esc_retry_done
                and time.time() - wait_start > 6.0
                and self._texts_match(
                    ["F：购买", "F:购买", "购买", "小壶商人"], texts, "any"
                )
            ):
                log("[导航] 仍在商店界面，补按 ESC×2", "INFO")
                self._execute_actions(
                    [
                        {
                            "type": "key",
                            "key_ref": "system_menu",
                            "repeat": 2,
                        }
                    ],
                    log,
                )
                shop_esc_retry_done = True
            if not silent and time.time() - last_progress_log >= 5.0:
                n = len(texts)
                hint = wait_hint or "目标界面"
                preview = " | ".join(str(t) for t in texts[:6]) if texts else "(无)"
                if len(preview) > 100:
                    preview = preview[:97] + "..."
                log(
                    f"[导航] 等待「{hint}」… OCR {n} 条: {preview}",
                    "INFO",
                )
                last_progress_log = time.time()
            time.sleep(self.poll_interval_s)
        if not silent and not optional:
            log(f"等待界面超时 ({timeout:.0f}s): {wait_hint}", "WARNING")
            jump = self._recover_unknown_screen(log)
            if jump is not None:
                self._pending_jump_index = jump
                texts = self._read_screen_texts()
                if detect and self._match_rule(detect, texts):
                    return True
                if detect_alt and self._match_rule(detect_alt, texts):
                    return True
        return False

    def _read_screen_texts(self) -> List[str]:
        self.repo_filter.refresh_window_info()
        image = self.repo_filter._capture_game_window()
        if image is None:
            return []
        try:
            result = self.ocr_engine.engine(image, use_det=True, use_cls=True)
            if result and result.txts:
                return [str(t).strip() for t in result.txts if str(t).strip()]
        except Exception as exc:
            log_debug(f"[导航OCR] {exc}")
        return []

    def _read_region_texts(self, rule: Optional[dict]) -> List[str]:
        if not rule:
            return []
        region = rule.get("region_ocr") or {}
        coords = region.get("coords_1080p")
        if not coords or len(coords) != 4:
            return []
        try:
            crop = self.repo_filter._capture_region(tuple(coords))
            if crop is None or crop.size == 0:
                return []
            result = self.ocr_engine.recognize_raw(crop)
            if result.get("success") and result.get("entries"):
                return [str(e) for e in result["entries"] if e]
        except Exception as exc:
            log_debug(f"[导航区域OCR] {exc}")
        return []

    def _match_rule(self, rule: dict, texts: List[str]) -> bool:
        if not rule:
            return False
        match_type = rule.get("match", "any")
        keywords = rule.get("texts", [])
        if not keywords:
            return False
        blob = "\n".join(texts)

        def hit(word: str) -> bool:
            return word in blob

        if match_type == "any":
            return any(hit(k) for k in keywords)
        if match_type == "all":
            return all(hit(k) for k in keywords)
        if match_type == "none":
            return not any(hit(k) for k in keywords)
        return False

    def _texts_match(
        self, keywords: List[str], texts: List[str], match_type: str
    ) -> bool:
        return self._match_rule({"match": match_type, "texts": keywords}, texts)
