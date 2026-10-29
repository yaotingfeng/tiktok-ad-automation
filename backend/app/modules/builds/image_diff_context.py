"""把试算对象名称与保护状态冻结到差异中；后续编辑不改变损失说明。"""


def freeze_diff_context(dramas, candidates, previous, music_titles=None):
    titles = {str(d.drama_id): d.title for d in dramas}
    names = {
        str(c.material_id): c.file_name_snapshot
        for group in candidates.values()
        for c in group
    }
    units = {str(u.id): u for u in previous}

    def describe(item, drama_id=None):
        value = dict(item) if isinstance(item, dict) else {"unit_id": str(item)}
        owner = str(value.get("drama_id") or drama_id or "")
        if owner:
            value.update(drama_id=owner, drama_title=titles.get(owner, "已移除剧目"))
        value["drama_titles"] = [
            titles.get(str(identity), "已移除剧目")
            for identity in value.get("drama_ids", [])
        ]
        unit = units.get(str(value.get("unit_id")))
        if unit:
            value.update(
                unit_position=unit.position,
                composition_protected=unit.composition_protected,
                music_protected=unit.music_protected,
                music_title=(music_titles or {}).get(str(unit.id)),
            )
        ids = value.get("material_ids") or (
            [value["material_id"]]
            if value.get("material_id")
            else list(unit.ordered_material_ids)
            if unit
            else []
        )
        value["materials"] = [
            {
                "material_id": str(identity),
                "file_name": names.get(str(identity), "图片记录已不可用"),
            }
            for identity in ids
        ]
        return value

    return describe
