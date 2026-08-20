private void showFullScreenChart(AbstractReport report, ComboLineColumnChartView v) {
        if (getView() == null) {
            return;
        }

        if (mFullScreenChartAnimator != null) {
            mFullScreenChartAnimator.cancel();
        }

        mCurrentFullScreenChart = v;

        ReportChartOptions options = loadReportChartOptions(getContext(), report);
        mFullScreenChart.setComboLineColumnChartData(report.getChartData(options));
        applyViewport(mFullScreenChart, false);

        // Calculate translation start and end point and scales.
        mCurrentFullScreenStartBounds = new Rect();
        final Rect finalBounds = new Rect();
        final Point globalOffset = new Point();

        mCurrentFullScreenChart.getGlobalVisibleRect(mCurrentFullScreenStartBounds);
        getView().getGlobalVisibleRect(finalBounds, globalOffset);
        mCurrentFullScreenStartBounds.offset(-globalOffset.x, -globalOffset.y);
        finalBounds.offset(-globalOffset.x, -globalOffset.y);

        mCurrentFullScreenStartScaleX = (float) mCurrentFullScreenStartBounds
                .width() / finalBounds.width();
        mCurrentFullScreenStartScaleY = (float) mCurrentFullScreenStartBounds
                .height() / finalBounds.height();

        // Hide the small chart and show the zoomed-in view. When the animation
        // begins, it will position the zoomed-in view in the place of the small
        // chart.
        mCurrentFullScreenChart.setVisibility(View.INVISIBLE);
        mFullScreenChartHolder.setVisibility(View.VISIBLE);

        // Set the pivot point for SCALE_X and SCALE_Y transformations to the
        // top-left corner of the zoomed-in view (the default is the center of
        // the view).
        mFullScreenChartHolder.setPivotX(0f);
        mFullScreenChartHolder.setPivotY(0f);

        // Construct and run the parallel animation of the four translation and
        // scale properties (X, Y, SCALE_X, and SCALE_Y).
        AnimatorSet set = new AnimatorSet();
        set.play(
                ObjectAnimator.ofFloat(mFullScreenChartHolder, View.X,
                        mCurrentFullScreenStartBounds.left, finalBounds.left))
                .with(ObjectAnimator.ofFloat(mFullScreenChartHolder, View.Y,
                        mCurrentFullScreenStartBounds.top, finalBounds.top))
                .with(ObjectAnimator.ofFloat(mFullScreenChartHolder, View.SCALE_X,
                        mCurrentFullScreenStartScaleX, 1f))
                .with(ObjectAnimator.ofFloat(mFullScreenChartHolder, View.SCALE_Y,
                        mCurrentFullScreenStartScaleY, 1f));
        set.setDuration(getResources().getInteger(
                android.R.integer.config_longAnimTime));
        set.addListener(new AnimatorListenerAdapter() {
            @Override
            public void onAnimationEnd(Animator animation) {
                mFullScreenChartAnimator = null;
                mAppBarLayout.setVisibility(View.INVISIBLE);
            }
        });
        set.start();
        mFullScreenChartAnimator = set;
    }