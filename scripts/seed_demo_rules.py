from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


RULES_PATH = Path(__file__).resolve().parent.parent / "rules.json"


def report(
    *,
    filters: list[str] | None = None,
    subtotal: bool = False,
    summary: bool = False,
    group_by: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "enabled": True,
        "auto_filter": True,
        "freeze_header": True,
        "filter_columns": filters or [],
        "subtotal": {
            "enabled": subtotal,
            "numeric_columns": "auto",
            "label": "Итого по выбранному фильтру",
        },
        "summary_sheet": {
            "enabled": summary,
            "group_by": group_by or filters or [],
            "numeric_columns": "auto",
            "sheet_name": "Итоги",
        },
    }


def make_rule(
    rule_id: str,
    name: str,
    domain: str,
    description: str,
    prompt: str,
    generated_rule: dict[str, Any],
    fingerprint: dict[str, Any],
) -> dict[str, Any]:
    now = datetime.now().isoformat(timespec="seconds")
    return {
        "id": rule_id,
        "type": "prompt_template",
        "name": name,
        "description": description,
        "domain": domain,
        "raw_user_prompt": prompt,
        "ai_improved_instruction": prompt,
        "prompt": prompt,
        "generated_rule": generated_rule,
        "fingerprint": fingerprint,
        "use_raw_data": True,
        "sheet_name": generated_rule.get("sheet_name"),
        "created_at": now,
        "updated_at": now,
        "is_demo_rule": True,
    }


DEMO_RULES = [
    make_rule(
        "demo-flat-clean-001",
        "Типовой: плоская таблица с первой строкой заголовков",
        "universal",
        "Для простых Excel-файлов, где заголовки находятся в первой строке.",
        "Прочитать первый лист как плоскую таблицу. Заголовки находятся в первой строке, данные начинаются со второй строки. Удалить полностью пустые строки и столбцы, нормализовать названия колонок, сохранить результат с автофильтром и закрепленной строкой заголовков.",
        {"table_type": "flat", "sheet_name": 0, "header_rows": [0], "data_start_row": 1, "drop_rows_contains": [], "excel_report": report()},
        {"table_type": "flat", "has_merged_cells": False, "has_month_like_columns": False, "has_total_rows": False, "header_depth": 1},
    ),
    make_rule(
        "demo-flat-service-rows-002",
        "Типовой: таблица со служебными строками сверху",
        "universal",
        "Для отчетов, где перед таблицей есть название, дата формирования и комментарии.",
        "Первые строки являются служебными. Заголовки таблицы находятся на строке 4, данные начинаются со строки 5. Удалить строки с примечаниями, датой формирования, комментариями и полностью пустые строки.",
        {"table_type": "flat", "sheet_name": 0, "header_rows": [3], "data_start_row": 4, "drop_rows_contains": ["примечание", "дата формирования", "сформировано", "комментарий"], "excel_report": report()},
        {"table_type": "flat", "has_merged_cells": False, "has_month_like_columns": False, "has_total_rows": True, "header_depth": 1},
    ),
    make_rule(
        "demo-multi-header-003",
        "Типовой: многоуровневые заголовки в 2 строки",
        "universal",
        "Для таблиц с группами колонок и подзаголовками.",
        "Обработать таблицу с многоуровневыми заголовками. Заголовки находятся на строках 2 и 3, данные начинаются со строки 4. Объединить уровни заголовков, удалить пустые строки и строки Итого.",
        {"table_type": "multi_header", "sheet_name": 0, "header_rows": [1, 2], "data_start_row": 3, "drop_rows_contains": ["итого", "всего"], "excel_report": report()},
        {"table_type": "multi_header", "has_merged_cells": True, "has_month_like_columns": False, "has_total_rows": True, "header_depth": 2},
    ),
    make_rule(
        "demo-month-cross-004",
        "Типовой: месяцы в колонках -> длинный формат",
        "finance",
        "Для бюджетов, планов и отчетов, где месяцы расположены по столбцам.",
        "Преобразовать широкую таблицу в длинный формат. Идентификаторы оставить как обычные колонки, месяцы из заголовков перенести в колонку Период, значения перенести в колонку Сумма. Удалить строки Итого и пустые значения.",
        {"table_type": "cross_table", "sheet_name": 0, "header_rows": [0], "data_start_row": 1, "drop_rows_contains": ["итого", "всего"], "id_columns": [{"name": "Статья"}, {"name": "Подразделение"}, {"name": "Проект"}], "value_columns": [], "melt": {"enabled": True, "var_name": "Период", "value_name": "Сумма"}, "excel_report": report(subtotal=True)},
        {"table_type": "cross_table", "has_merged_cells": False, "has_month_like_columns": True, "has_total_rows": True, "header_depth": 1},
    ),
    make_rule(
        "demo-sales-branch-005",
        "Продажи: фильтр по филиалам и итоги",
        "sales",
        "Для отчетов продаж с филиалами, товарами и суммами.",
        "Нормализовать отчет продаж. Сохранить филиал, товар, дату, менеджера и сумму продажи. Удалить служебные строки и строки Итого. В итоговом Excel включить автофильтр, строку SUBTOTAL и отдельный лист Итоги по филиалам.",
        {"table_type": "flat", "sheet_name": 0, "header_rows": [0], "data_start_row": 1, "drop_rows_contains": ["итого", "всего", "служебная"], "excel_report": report(filters=["Филиал"], subtotal=True, summary=True, group_by=["Филиал"])},
        {"table_type": "flat", "has_merged_cells": False, "has_month_like_columns": False, "has_total_rows": True, "header_depth": 1},
    ),
    make_rule(
        "demo-inventory-006",
        "Склад: остатки по складам",
        "warehouse",
        "Для складских ведомостей с остатками и движением товаров.",
        "Обработать складскую ведомость. Оставить товар, артикул, склад, единицу измерения, начальный остаток, приход, расход и конечный остаток. Удалить пустые строки, подытоги и строки Итого.",
        {"table_type": "flat", "sheet_name": 0, "header_rows": [0], "data_start_row": 1, "drop_rows_contains": ["итого", "подытог", "всего"], "excel_report": report(filters=["Склад", "Товар"])},
        {"table_type": "flat", "has_merged_cells": False, "has_month_like_columns": False, "has_total_rows": True, "header_depth": 1},
    ),
    make_rule(
        "demo-production-daily-007",
        "Производство: дневной выпуск",
        "production",
        "Для ежедневных производственных отчетов.",
        "Нормализовать дневной производственный отчет. Сохранить дату, участок, смену, изделие, план, факт, брак и комментарий. Удалить служебные строки, пустые строки и итоги.",
        {"table_type": "flat", "sheet_name": 0, "header_rows": [2], "data_start_row": 3, "drop_rows_contains": ["итого", "начальник смены", "сформировано"], "excel_report": report(filters=["Участок", "Смена"], subtotal=True)},
        {"table_type": "flat", "has_merged_cells": False, "has_month_like_columns": False, "has_total_rows": True, "header_depth": 1},
    ),
    make_rule(
        "demo-hr-timesheet-008",
        "HR: табель сотрудников по дням",
        "hr",
        "Для табелей, где дни месяца расположены в колонках.",
        "Преобразовать табель сотрудников из широкого формата в длинный. Сотрудник, табельный номер и подразделение остаются идентификаторами. Дни месяца перенести в колонку День, часы или отметки перенести в колонку Значение.",
        {"table_type": "cross_table", "sheet_name": 0, "header_rows": [0], "data_start_row": 1, "drop_rows_contains": ["итого", "всего"], "id_columns": [{"name": "Сотрудник"}, {"name": "Табельный номер"}, {"name": "Подразделение"}], "value_columns": [], "melt": {"enabled": True, "var_name": "День", "value_name": "Значение"}, "excel_report": report(filters=["Подразделение", "Сотрудник"])},
        {"table_type": "cross_table", "has_merged_cells": False, "has_month_like_columns": True, "has_total_rows": True, "header_depth": 1},
    ),
    make_rule(
        "demo-bank-statement-009",
        "Финансы: банковская выписка",
        "finance",
        "Для банковских выписок и реестров платежей.",
        "Очистить банковскую выписку. Оставить дату операции, контрагента, назначение платежа, дебет, кредит, сумму и счет. Удалить шапку банка, пустые строки, остатки на начало и конец, строки Итого.",
        {"table_type": "flat", "sheet_name": 0, "header_rows": [0], "data_start_row": 1, "drop_rows_contains": ["остаток на начало", "остаток на конец", "итого", "банк"], "excel_report": report()},
        {"table_type": "flat", "has_merged_cells": False, "has_month_like_columns": False, "has_total_rows": True, "header_depth": 1},
    ),
    make_rule(
        "demo-expense-010",
        "Финансы: авансовые расходы по статьям",
        "finance",
        "Для отчетов расходов с категориями и подразделениями.",
        "Нормализовать отчет расходов. Сохранить дату, подразделение, сотрудника, статью расхода, описание и сумму. Удалить служебные строки и итоги. Добавить лист Итоги по подразделениям и статьям расходов.",
        {"table_type": "flat", "sheet_name": 0, "header_rows": [0], "data_start_row": 1, "drop_rows_contains": ["итого", "всего", "подотчет"], "excel_report": report(filters=["Подразделение", "Статья расхода"], subtotal=True, summary=True, group_by=["Подразделение", "Статья расхода"])},
        {"table_type": "flat", "has_merged_cells": False, "has_month_like_columns": False, "has_total_rows": True, "header_depth": 1},
    ),
    make_rule(
        "demo-price-list-011",
        "Прайс-лист: очистка и нормализация",
        "commerce",
        "Для прайс-листов с категориями, артикулами и ценами.",
        "Очистить прайс-лист. Оставить категорию, артикул, наименование, единицу измерения, цену и валюту. Удалить рекламные блоки, пустые строки, примечания и итоги.",
        {"table_type": "flat", "sheet_name": 0, "header_rows": [0], "data_start_row": 1, "drop_rows_contains": ["скидка действует", "примечание", "итого"], "excel_report": report(filters=["Категория", "Валюта"])},
        {"table_type": "flat", "has_merged_cells": False, "has_month_like_columns": False, "has_total_rows": False, "header_depth": 1},
    ),
    make_rule(
        "demo-orders-012",
        "Заказы: реестр заказов клиентов",
        "commerce",
        "Для списков заказов с клиентами, статусами и суммами.",
        "Нормализовать реестр заказов. Сохранить номер заказа, дату, клиента, менеджера, статус, сумму и комментарий. Удалить служебные строки и итоги. Включить фильтр по статусу, клиенту и менеджеру.",
        {"table_type": "flat", "sheet_name": 0, "header_rows": [0], "data_start_row": 1, "drop_rows_contains": ["итого", "всего заказов"], "excel_report": report(filters=["Статус", "Клиент", "Менеджер"])},
        {"table_type": "flat", "has_merged_cells": False, "has_month_like_columns": False, "has_total_rows": True, "header_depth": 1},
    ),
    make_rule(
        "demo-quality-013",
        "Качество: дефекты по участкам",
        "quality",
        "Для журналов дефектов и несоответствий.",
        "Обработать журнал качества. Оставить дату, участок, изделие, тип дефекта, количество, ответственного и статус. Удалить служебные строки и итоги. Добавить фильтры и лист Итоги по участкам и типам дефектов.",
        {"table_type": "flat", "sheet_name": 0, "header_rows": [0], "data_start_row": 1, "drop_rows_contains": ["итого", "всего"], "excel_report": report(filters=["Участок", "Тип дефекта", "Статус"], subtotal=True, summary=True, group_by=["Участок", "Тип дефекта"])},
        {"table_type": "flat", "has_merged_cells": False, "has_month_like_columns": False, "has_total_rows": True, "header_depth": 1},
    ),
    make_rule(
        "demo-project-plan-014",
        "Проекты: план-факт по этапам",
        "project",
        "Для проектных таблиц с планом, фактом и отклонением.",
        "Нормализовать проектный план-факт. Сохранить проект, этап, ответственного, дату начала, дату окончания, план, факт и отклонение. Удалить групповые заголовки, пустые строки и итоги.",
        {"table_type": "flat", "sheet_name": 0, "header_rows": [1], "data_start_row": 2, "drop_rows_contains": ["итого", "всего", "этапы проекта"], "excel_report": report(filters=["Проект", "Ответственный"])},
        {"table_type": "flat", "has_merged_cells": True, "has_month_like_columns": False, "has_total_rows": True, "header_depth": 1},
    ),
    make_rule(
        "demo-survey-015",
        "Анкеты: ответы респондентов",
        "analytics",
        "Для выгрузок анкет и опросов.",
        "Очистить выгрузку анкет. Сохранить ID респондента, дату ответа и сегмент. Если вопросы расположены в колонках, преобразовать их в длинный формат: вопрос в отдельную колонку, ответ в отдельную колонку. Удалить пустые ответы.",
        {"table_type": "cross_table", "sheet_name": 0, "header_rows": [0], "data_start_row": 1, "drop_rows_contains": [], "id_columns": [{"name": "ID"}, {"name": "Дата"}, {"name": "Сегмент"}], "value_columns": [], "melt": {"enabled": True, "var_name": "Вопрос", "value_name": "Ответ"}, "excel_report": report(filters=["Сегмент"])},
        {"table_type": "cross_table", "has_merged_cells": False, "has_month_like_columns": False, "has_total_rows": False, "header_depth": 1},
    ),
    make_rule(
        "oil-production-daily-016",
        "Нефтегаз: суточная добыча по скважинам",
        "oil_and_gas",
        "Для суточных отчетов добычи нефти, жидкости, газа и воды по скважинам.",
        "Нормализовать суточный отчет добычи. Сохранить месторождение, куст, скважину, дату, нефть, жидкость, газ, воду, обводненность, дебит и режим работы. Удалить строки Итого, Всего, подытоги по кустам и служебные комментарии. В итоговом Excel включить фильтр по месторождению, кусту и скважине, добавить итоги по числовым колонкам.",
        {"table_type": "flat", "sheet_name": 0, "header_rows": [0], "data_start_row": 1, "drop_rows_contains": ["итого", "всего", "подытог", "комментарий"], "excel_report": report(filters=["Месторождение", "Куст", "Скважина"], subtotal=True, summary=True, group_by=["Месторождение", "Куст"])},
        {"table_type": "flat", "has_merged_cells": False, "has_month_like_columns": False, "has_total_rows": True, "header_depth": 1},
    ),
    make_rule(
        "oil-well-fund-017",
        "Нефтегаз: фонд скважин",
        "oil_and_gas",
        "Для ведомостей эксплуатационного фонда скважин.",
        "Обработать ведомость фонда скважин. Оставить месторождение, куст, номер скважины, тип скважины, способ эксплуатации, состояние, дата ввода, пласт, текущий дебит и примечание. Удалить служебные строки и итоги. Включить фильтры по месторождению, типу скважины, способу эксплуатации и состоянию.",
        {"table_type": "flat", "sheet_name": 0, "header_rows": [0], "data_start_row": 1, "drop_rows_contains": ["итого", "всего", "справочно"], "excel_report": report(filters=["Месторождение", "Тип скважины", "Способ эксплуатации", "Состояние"])},
        {"table_type": "flat", "has_merged_cells": False, "has_month_like_columns": False, "has_total_rows": True, "header_depth": 1},
    ),
    make_rule(
        "oil-drilling-report-018",
        "Нефтегаз: бурение и проходка",
        "oil_and_gas",
        "Для отчетов бурения с планом, фактом и проходкой.",
        "Нормализовать отчет бурения. Сохранить скважину, куст, подрядчика, дату, интервал от, интервал до, проходку за сутки, накопленную проходку, плановую глубину, фактическую глубину, этап работ и статус. Удалить пустые строки, итоги и текстовые шапки. Добавить фильтр по подрядчику, кусту, скважине и этапу работ.",
        {"table_type": "flat", "sheet_name": 0, "header_rows": [1], "data_start_row": 2, "drop_rows_contains": ["итого", "всего", "сводка"], "excel_report": report(filters=["Подрядчик", "Куст", "Скважина", "Этап работ"], subtotal=True)},
        {"table_type": "flat", "has_merged_cells": True, "has_month_like_columns": False, "has_total_rows": True, "header_depth": 1},
    ),
    make_rule(
        "oil-workover-019",
        "Нефтегаз: КРС и ПРС",
        "oil_and_gas",
        "Для планов и факта капитального/подземного ремонта скважин.",
        "Обработать таблицу КРС/ПРС. Оставить скважину, куст, вид ремонта, бригаду, подрядчика, дату начала, дату окончания, длительность, причину ремонта, результат и комментарий. Удалить служебные строки, итоги и пустые строки. Добавить фильтры по виду ремонта, подрядчику и бригаде.",
        {"table_type": "flat", "sheet_name": 0, "header_rows": [0], "data_start_row": 1, "drop_rows_contains": ["итого", "всего", "примечание"], "excel_report": report(filters=["Вид ремонта", "Подрядчик", "Бригада"], subtotal=True)},
        {"table_type": "flat", "has_merged_cells": False, "has_month_like_columns": False, "has_total_rows": True, "header_depth": 1},
    ),
    make_rule(
        "oil-downtime-020",
        "Нефтегаз: простои скважин и оборудования",
        "oil_and_gas",
        "Для журналов простоев с причинами и длительностью.",
        "Нормализовать журнал простоев. Сохранить дату, месторождение, куст, скважину или объект, оборудование, причина простоя, начало, окончание, длительность, ответственное подразделение и комментарий. Удалить строки Итого и служебные блоки. Добавить лист Итоги по причинам простоев и подразделениям.",
        {"table_type": "flat", "sheet_name": 0, "header_rows": [0], "data_start_row": 1, "drop_rows_contains": ["итого", "всего", "свод"], "excel_report": report(filters=["Месторождение", "Причина простоя", "Подразделение"], subtotal=True, summary=True, group_by=["Причина простоя", "Подразделение"])},
        {"table_type": "flat", "has_merged_cells": False, "has_month_like_columns": False, "has_total_rows": True, "header_depth": 1},
    ),
    make_rule(
        "oil-injection-021",
        "Нефтегаз: закачка воды по нагнетательным скважинам",
        "oil_and_gas",
        "Для отчетов ППД и нагнетательного фонда.",
        "Обработать отчет по закачке воды. Сохранить месторождение, куст, нагнетательную скважину, пласт, дату, закачку за сутки, давление устьевое, давление пластовое, приемистость и режим. Удалить строки Итого и пустые строки. Включить фильтры по месторождению, кусту, пласту и скважине.",
        {"table_type": "flat", "sheet_name": 0, "header_rows": [0], "data_start_row": 1, "drop_rows_contains": ["итого", "всего", "подытог"], "excel_report": report(filters=["Месторождение", "Куст", "Пласт", "Скважина"], subtotal=True, summary=True, group_by=["Месторождение", "Пласт"])},
        {"table_type": "flat", "has_merged_cells": False, "has_month_like_columns": False, "has_total_rows": True, "header_depth": 1},
    ),
    make_rule(
        "oil-lab-quality-022",
        "Нефтегаз: лабораторное качество нефти",
        "oil_and_gas",
        "Для лабораторных анализов нефти, воды и газа.",
        "Очистить лабораторный отчет качества. Оставить дату отбора, объект, скважину или резервуар, плотность, серу, воду, соли, механические примеси, температуру и комментарий. Удалить шапку лаборатории, подписи и пустые строки. Добавить фильтр по объекту и типу пробы.",
        {"table_type": "flat", "sheet_name": 0, "header_rows": [2], "data_start_row": 3, "drop_rows_contains": ["лаборатория", "подпись", "итого"], "excel_report": report(filters=["Объект", "Тип пробы"], subtotal=False)},
        {"table_type": "flat", "has_merged_cells": True, "has_month_like_columns": False, "has_total_rows": False, "header_depth": 1},
    ),
    make_rule(
        "oil-pipeline-balance-023",
        "Нефтегаз: баланс транспорта нефти",
        "oil_and_gas",
        "Для балансов приема, сдачи и транспортировки нефти.",
        "Нормализовать баланс транспорта нефти. Сохранить дату, пункт приема, пункт сдачи, трубопровод, объем приема, объем сдачи, потери, плотность, масса и комментарий. Удалить служебные строки, итоги и пустые строки. Добавить фильтры и лист Итоги по пунктам приема/сдачи.",
        {"table_type": "flat", "sheet_name": 0, "header_rows": [0], "data_start_row": 1, "drop_rows_contains": ["итого", "всего", "баланс"], "excel_report": report(filters=["Пункт приема", "Пункт сдачи", "Трубопровод"], subtotal=True, summary=True, group_by=["Пункт приема", "Пункт сдачи"])},
        {"table_type": "flat", "has_merged_cells": False, "has_month_like_columns": False, "has_total_rows": True, "header_depth": 1},
    ),
    make_rule(
        "oil-monthly-production-cross-024",
        "Нефтегаз: месячная добыча в колонках",
        "oil_and_gas",
        "Для широких отчетов, где месяцы находятся в столбцах.",
        "Преобразовать месячный отчет добычи из широкого формата в длинный. Месторождение, куст, скважина и показатель оставить идентификаторами. Месяцы из колонок перенести в колонку Период, значения добычи перенести в колонку Значение. Удалить строки Итого, Всего и пустые значения.",
        {"table_type": "cross_table", "sheet_name": 0, "header_rows": [0], "data_start_row": 1, "drop_rows_contains": ["итого", "всего"], "id_columns": [{"name": "Месторождение"}, {"name": "Куст"}, {"name": "Скважина"}, {"name": "Показатель"}], "value_columns": [], "melt": {"enabled": True, "var_name": "Период", "value_name": "Значение"}, "excel_report": report(filters=["Месторождение", "Куст", "Скважина", "Показатель"], subtotal=True, summary=True, group_by=["Месторождение", "Показатель"])},
        {"table_type": "cross_table", "has_merged_cells": False, "has_month_like_columns": True, "has_total_rows": True, "header_depth": 1},
    ),
    make_rule(
        "oil-well-tests-025",
        "Нефтегаз: исследования скважин",
        "oil_and_gas",
        "Для таблиц ГДИС/замеров и результатов исследований скважин.",
        "Обработать результаты исследований скважин. Сохранить дату, месторождение, куст, скважину, тип исследования, пластовое давление, забойное давление, дебит жидкости, дебит нефти, обводненность, коэффициент продуктивности и комментарий. Удалить служебные строки и итоги.",
        {"table_type": "flat", "sheet_name": 0, "header_rows": [0], "data_start_row": 1, "drop_rows_contains": ["итого", "всего", "примечание"], "excel_report": report(filters=["Месторождение", "Куст", "Скважина", "Тип исследования"], subtotal=True)},
        {"table_type": "flat", "has_merged_cells": False, "has_month_like_columns": False, "has_total_rows": True, "header_depth": 1},
    ),
]


def main() -> None:
    try:
        rules = json.loads(RULES_PATH.read_text(encoding="utf-8"))
        if not isinstance(rules, list):
            rules = []
    except FileNotFoundError:
        rules = []

    demo_ids = {item["id"] for item in DEMO_RULES}
    preserved = [item for item in rules if item.get("id") not in demo_ids and not item.get("is_demo_rule")]
    preserved.extend(DEMO_RULES)
    RULES_PATH.write_text(json.dumps(preserved, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"demo_rules={len(DEMO_RULES)}, total={len(preserved)}")


if __name__ == "__main__":
    main()
