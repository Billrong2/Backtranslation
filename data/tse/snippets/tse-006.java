/**
     * Create a new HealthReport.
     *
     * @param score       The percentage health score (from 0 to 100 inclusive).
     * @param iconUrl     The path to the icon corresponding to this {@link Action}'s health or <code>null</code> to
     *                    display the default icon corresponding to the current health score.
     *                    <p/>
     *                    If the path begins with a '/' then it will be the absolute path, otherwise the image is
     *                    assumed to be in one of <code>/images/16x16/</code>, <code>/images/24x24/</code> or
     *                    <code>/images/32x32/</code> depending on the icon size selected by the user.
     *                    When calculating the url to display for absolute paths, the getIconUrl(String) method
     *                    will replace /32x32/ in the path with the appropriate size.
     * @param description The health icon's tool-tip.
     */
    public HealthReport(int score, String iconUrl, Localizable description) {
        this.score = score;
        if (score <= 20) {
            this.iconClassName = HEALTH_0_TO_20;
        } else if (score <= 40) {
            this.iconClassName = HEALTH_21_TO_40;
        } else if (score <= 60) {
            this.iconClassName = HEALTH_41_TO_60;
        } else if (score <= 80) {
            this.iconClassName = HEALTH_61_TO_80;
        } else {
            this.iconClassName = HEALTH_OVER_80;
        }
        if (iconUrl == null) {
            if (score <= 20) {
                this.iconUrl = HEALTH_0_TO_20_IMG;
            } else if (score <= 40) {
                this.iconUrl = HEALTH_21_TO_40_IMG;
            } else if (score <= 60) {
                this.iconUrl = HEALTH_41_TO_60_IMG;
            } else if (score <= 80) {
                this.iconUrl = HEALTH_61_TO_80_IMG;
            } else {
                this.iconUrl = HEALTH_OVER_80_IMG;
            }
        } else {
            this.iconUrl = iconUrl;
        }
        this.description = null;
        setLocalizibleDescription(description);
    }