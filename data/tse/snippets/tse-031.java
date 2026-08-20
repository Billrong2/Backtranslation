/**
     * @see org.opencms.ade.sitemap.client.hoverbar.I_CmsContextMenuItemProvider#createContextMenu(org.opencms.ade.sitemap.client.hoverbar.CmsSitemapHoverbar)
     */
    public List<A_CmsSitemapMenuEntry> createContextMenu(CmsSitemapHoverbar hoverbar) {

        List<A_CmsSitemapMenuEntry> result = Lists.newArrayList();

        result.add(new CmsGotoMenuEntry(hoverbar));
        result.add(new CmsGotoExplorerMenuEntry(hoverbar));
        result.add(new CmsOpenGalleryMenuEntry(hoverbar));
        result.add(new CmsEditRedirectMenuEntry(hoverbar));
        result.add(new CmsEditModelPageMenuEntry(hoverbar));
        result.add(new CmsDeleteModelPageMenuEntry(hoverbar));
        result.add(new CmsDisableMenuEntry(hoverbar));
        result.add(new CmsEditMenuEntry(hoverbar));
        result.add(new CmsCopyPageMenuEntry(hoverbar));
        result.add(new CmsCopyModelPageMenuEntry(hoverbar));
        result.add(new CmsSetDefaultModelMenuEntry(hoverbar));
        result.add(new CmsCopyAsModelGroupPageMenuEntry(hoverbar));
        result.add(new CmsCreateGalleryMenuEntry(hoverbar));
        result.add(new CmsResourceInfoMenuEntry(hoverbar));
        result.add(new CmsParentSitemapMenuEntry(hoverbar));
        result.add(new CmsGotoSubSitemapMenuEntry(hoverbar));
        result.add(new CmsNewChoiceMenuEntry(hoverbar));
        result.add(new CmsHideMenuEntry(hoverbar));
        result.add(new CmsShowMenuEntry(hoverbar));
        result.add(new CmsAddToNavMenuEntry(hoverbar));
        result.add(new CmsBumpDetailPageMenuEntry(hoverbar));
        result.add(new CmsRefreshMenuEntry(hoverbar));
        result.add(
            new CmsAdvancedSubmenu(
                hoverbar,
                Arrays.asList(
                    new CmsAvailabilityMenuEntry(hoverbar),
                    new CmsLockReportMenuEntry(hoverbar),
                    new CmsSeoMenuEntry(hoverbar),
                    new CmsSubSitemapMenuEntry(hoverbar),
                    new CmsMergeMenuEntry(hoverbar),
                    new CmsRemoveMenuEntry(hoverbar))));
        result.add(new CmsModelPageLockReportMenuEntry(hoverbar));
        result.add(new CmsDeleteMenuEntry(hoverbar));

        return result;
    }