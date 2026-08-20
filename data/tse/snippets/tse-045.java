private void updateOrClearHeader(int firstVisiblePosition) {
        final int adapterCount = mAdapter == null ? 0 : mAdapter.getCount();
        if (adapterCount == 0 || !mAreHeadersSticky) {
            return;
        }

        final int headerViewCount = mList.getHeaderViewsCount();
        int headerPosition = firstVisiblePosition - headerViewCount;
        if (mList.getChildCount() > 0) {
            View firstItem = mList.getChildAt(0);
            if (firstItem.getBottom() < stickyHeaderTop()) {
                headerPosition++;
            }
        }

        // It is not a mistake to call getFirstVisiblePosition() here.
        // Most of the time getFixedFirstVisibleItem() should be called
        // but that does not work great together with getChildAt()
        final boolean doesListHaveChildren = mList.getChildCount() != 0;
        final boolean isFirstViewBelowTop = doesListHaveChildren
                && mList.getFirstVisiblePosition() == 0
                && mList.getChildAt(0).getTop() >= stickyHeaderTop();
        final boolean isHeaderPositionOutsideAdapterRange = headerPosition > adapterCount - 1
                || headerPosition < 0;
        if (!doesListHaveChildren || isHeaderPositionOutsideAdapterRange || isFirstViewBelowTop) {
            clearHeader();
            return;
        }

        updateHeader(headerPosition);
    }