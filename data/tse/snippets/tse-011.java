private void handleFABClick(final int edit, final int otherType) {
        closeFABMenu();

        Preferences prefs = new Preferences(this);
        CarCursor car = new CarSelection().suspendedSince((Date) null).query(getContentResolver(),
                CarColumns.ALL_COLUMNS, CarColumns.NAME + " COLLATE UNICODE");

        if (car.getCount() == 1 || !prefs.isShowCarMenu()) {
            Intent intent = getDetailActivityIntent(edit, prefs.getDefaultCar(), otherType);
            startActivityForResult(intent, REQUEST_ADD_DATA + edit);
        } else {
            final long[] carIds = new long[car.getCount()];
            final String[] carNames = new String[car.getCount()];
            while (car.moveToNext()) {
                carIds[car.getPosition()] = car.getId();
                carNames[car.getPosition()] = car.getName();
            }

            new AlertDialog.Builder(this)
                    .setItems(carNames, new DialogInterface.OnClickListener() {
                        @Override
                        public void onClick(DialogInterface dialog, int which) {
                            Intent intent = getDetailActivityIntent(edit, carIds[which], otherType);
                            startActivityForResult(intent, REQUEST_ADD_DATA + edit);
                        }
                    })
                    .create()
                    .show();
        }
    }