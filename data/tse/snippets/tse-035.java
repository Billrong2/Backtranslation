/**
     * @see org.opencms.workplace.tools.I_CmsHtmlIconButton#buttonHtml(CmsWorkplace)
     */
    @Override
    public String buttonHtml(CmsWorkplace wp) {

        if (!isVisible()) {
            return "";
        }
        if (isEnabled()) {
            String onClic = "listRSelMAction('"
                + getListId()
                + "','"
                + getId()
                + "', '"
                + CmsStringUtil.escapeJavaScript(wp.resolveMacros(getConfirmationMessage().key(wp.getLocale())))
                + "', "
                + CmsHtmlList.NO_SELECTION_MATCH_HELP_VAR
                + getId()
                + ", '"
                + getRelatedActionIds()
                + "');";
            return A_CmsHtmlIconButton.defaultButtonHtml(
                CmsHtmlIconButtonStyleEnum.SMALL_ICON_TEXT,
                getId(),
                getName().key(wp.getLocale()),
                getHelpText().key(wp.getLocale()),
                isEnabled(),
                getIconPath(),
                null,
                onClic);
        }
        return "";
    }