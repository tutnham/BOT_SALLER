"""Labels of the persistent reply keyboard for owners and employees."""

OWNER_MENU = "Меню"
OWNER_REPORT_DAY = "Отчёт за день"
OWNER_REPORT_WEEK = "Отчёт за неделю"
OWNER_MARKUP = "Наценки"

EMP_NEW_REQUEST = "Новый запрос"
EMP_MY_REQUESTS = "Мои заявки"

OWNER_BUTTONS = frozenset(
    {OWNER_MENU, OWNER_REPORT_DAY, OWNER_REPORT_WEEK, OWNER_MARKUP}
)
EMPLOYEE_BUTTONS = frozenset({EMP_NEW_REQUEST, EMP_MY_REQUESTS})
