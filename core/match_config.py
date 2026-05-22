"""正面词条匹配数量配置（单有效 / 双有效 / 三有效）"""

from typing import Dict


def get_required_positive_matches(settings: Dict, *, for_shop: bool = False) -> int:
    """
    从设置中读取需要的白名单正面匹配条数（1、2 或 3）。

    兼容旧版布尔项 require_double_valid / shop_require_double_valid。
    """
    key = "shop_positive_matches" if for_shop else "repo_positive_matches"
    legacy_key = "shop_require_double_valid" if for_shop else "require_double_valid"

    if key in settings:
        value = int(settings[key])
        if value in (1, 2, 3):
            return value

    if legacy_key in settings:
        return 2 if settings[legacy_key] else 3

    return 2


def cannot_meet_match_requirement(positive_count: int, required_matches: int) -> bool:
    """正面词条数量不足以满足匹配要求时，提前判定不合格。"""
    if positive_count == 0:
        return True
    return positive_count < required_matches
