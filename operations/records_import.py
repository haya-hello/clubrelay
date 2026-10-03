"""负责人明确选择的结构化证据导入，非 AI 推断。 / Explicit manager-selected structured imports, not AI inference."""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction

from .models import Audit, Evidence, Material, Person
from .people import _dedupe_key, _iso_date, save_evidence
from .security import is_manager


_MAPPED_FIELDS = ("person_id", "title", "occurred_on", "quote")
_REQUIRED_FIELDS = ("person_id", "title", "quote")


def _cell(value):
    return "" if value is None else str(value).strip()


def _table_mapping(material, mapping, table_index):
    if not isinstance(mapping, dict) or any(not mapping.get(field) for field in _REQUIRED_FIELDS):
        raise ValidationError("请映射稳定成员编号、事项标题和逐字引用三列。")
    if not isinstance(material.tables, list):
        raise ValidationError("资料没有可用的结构化表格。")
    if not isinstance(table_index, int) or isinstance(table_index, bool) or not 0 <= table_index < len(material.tables):
        raise ValidationError("所选表格不存在，请重新确认。")
    table = material.tables[table_index]
    if not isinstance(table, dict) or not isinstance(table.get("columns"), list) or not isinstance(table.get("rows"), list):
        raise ValidationError("表格结构不完整，请重新解析资料。")
    columns = table["columns"]
    indexes = {}
    for field in _MAPPED_FIELDS:
        name = mapping.get(field)
        if not name:
            continue
        if columns.count(name) != 1:
            raise ValidationError(f"映射列“{name}”不存在或重名，请重新确认。")
        indexes[field] = columns.index(name)
    return table["rows"], indexes


def _quote_anchor(material, quote):
    # 只复用包含完整引用的既有定位，不编造表格页码或资料位置。
    # Reuse only an existing segment containing the exact quote; never invent locations.
    for segment in material.segments:
        if isinstance(segment, dict) and quote in str(segment.get("text", "")):
            return str(segment.get("anchor", ""))[:Evidence._meta.get_field("anchor").max_length]
    return ""


@transaction.atomic
def import_observations(user, material, mapping, kind, confidence="self_report", table_index=0, verified_ack=False):
    """逐行导入负责人选择的数据，保留错误并共享证据服务的去重规则。

    Import manager-selected rows, collecting errors and sharing evidence deduplication.
    """
    if not is_manager(user):
        raise PermissionDenied("仅有效的社团负责人可以批量导入成员证据。")
    if kind not in Evidence.Kind.values:
        raise ValidationError("请选择有效的证据维度，不将消息数量推定为贡献。")
    if confidence not in Evidence.Confidence.values:
        raise ValidationError("请选择有效的证据状态。")
    if confidence == Evidence.Confidence.VERIFIED and verified_ack is not True:
        raise ValidationError("批量标为负责人核实前，必须明确确认已核对这批记录。")
    try:
        material = Material.objects.select_for_update().select_related("event").get(pk=material.pk if hasattr(material, "pk") else material)
    except (Material.DoesNotExist, ValueError, TypeError, ValidationError) as exc:
        raise ValidationError("来源资料不存在，请重新选择。") from exc
    if material.excluded or material.parse_status != "parsed":
        raise ValidationError("只能从已解析且未排除的资料导入成员证据。")
    rows, indexes = _table_mapping(material, mapping, table_index)
    result = {"created": 0, "duplicates": 0, "issues": []}

    for row_number, row in enumerate(rows, start=2):
        if not isinstance(row, (list, tuple)):
            result["issues"].append(f"第 {row_number} 行：结构无效，未导入。")
            continue
        values = {field: _cell(row[index]) if index < len(row) else "" for field, index in indexes.items()}
        if not any(values.values()):
            continue
        try:
            # 每行有独立保存点；单行内容错误不能撤回其他有效记录。
            # Each row has its own savepoint so validation errors do not undo valid rows.
            with transaction.atomic():
                if not values["person_id"]:
                    raise ValidationError("缺少稳定成员编号，不按姓名猜测身份。")
                person = Person.objects.filter(external_id=values["person_id"]).first()
                if person is None:
                    raise ValidationError("成员编号不在当前名册中，请先核对或导入名册；未自动创建成员。")
                for field, label in (("title", "事项标题"), ("quote", "逐字引用")):
                    maximum = Evidence._meta.get_field(field).max_length
                    if not values[field]:
                        raise ValidationError(f"{label}不能为空。")
                    if len(values[field]) > maximum:
                        raise ValidationError(f"{label}超过 {maximum} 字限制，未自动截断证据。")
                if values["quote"] not in material.text:
                    raise ValidationError("引用未逐字出现在资料正文中，未据此建立证据。")
                occurred_on = _iso_date(values.get("occurred_on"), "发生日期")
                evidence_data = {
                    "member": person, "event": material.event, "material": material,
                    "title": values["title"], "kind": kind, "confidence": confidence,
                    "occurred_on": occurred_on, "quote": values["quote"],
                    "anchor": _quote_anchor(material, values["quote"]),
                    "note": "负责人明确选择的结构化资料批量导入；不是 AI 推断。",
                }
                # 与单条保存共用键，避免批量导入自行改变事件数量或可信度。
                # Share the single-record key so batch imports cannot change event counts or confidence.
                existed = Evidence.objects.filter(dedupe_key=_dedupe_key(evidence_data)).exists()
                save_evidence(user, evidence_data)
                result["duplicates" if existed else "created"] += 1
        except ValidationError as exc:
            result["issues"].append(f"第 {row_number} 行：{'；'.join(exc.messages)}")
        except IntegrityError:
            result["issues"].append(f"第 {row_number} 行：记录在处理期间变化，请重试并核对是否重复。")

    Audit.objects.create(
        actor=user, action="import_observations", object_id=str(material.pk),
        detail=f"负责人选择的结构化导入，非 AI；维度 {kind}，状态 {confidence}，新增 {result['created']}，重复 {result['duplicates']}，问题 {len(result['issues'])}。",
    )
    return result
