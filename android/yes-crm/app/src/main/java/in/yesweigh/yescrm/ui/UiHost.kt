package `in`.yesweigh.yescrm.ui

interface UiHost {
    fun navigateTo(page: Page)
    fun openLeaveDialog()
    fun launchWhatsApp()
    fun showToast(message: String)
    fun openNotices()
    fun openProfile()
}

enum class Page {
    HOME,
    WHATSAPP,
    CALLS,
    AI,
    ATTENDANCE,
}
