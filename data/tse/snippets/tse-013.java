private boolean save() {
        FormValidator validator = new FormValidator();
        validator.add(new FormFieldNotEmptyValidator(mEdtName));
        validator.add(new AbstractFormFieldValidator(mEdtName) {
            @Override
            protected boolean isValid() {
                String name = mEdtName.getText().toString();
                return !mOtherFuelTypeNames.contains(name);
            }

            @Override
            protected int getMessage() {
                return R.string.validate_error_fuel_type_exists;
            }
        });
        validator.add(new FormFieldNotEmptyValidator(mEdtCategory));

        if (validator.validate()) {
            FuelTypeContentValues values = new FuelTypeContentValues();
            values.putName(mEdtName.getText().toString());
            values.putCategory(mEdtCategory.getText().toString());

            if (mFuelType == null) {
                values.insert(getActivity().getContentResolver());
            } else {
                FuelTypeSelection where = new FuelTypeSelection().id(mFuelType.getId());
                values.update(getActivity().getContentResolver(), where);
            }

            return true;
        } else {
            return false;
        }
    }