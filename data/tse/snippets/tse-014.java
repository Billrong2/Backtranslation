@Override
    public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_setup_webdav_sync);
        getWindow().setLayout(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);

        mEdtUrl = (EditText) findViewById(R.id.edt_url);
        mEdtUrl.addTextChangedListener(new TextWatcher() {
            @Override
            public void beforeTextChanged(CharSequence s, int start, int count, int after) {
            }

            @Override
            public void onTextChanged(CharSequence s, int start, int before, int count) {
            }

            @Override
            public void afterTextChanged(Editable s) {
                mTxtTrustCertificateDescription.setVisibility(View.GONE);
                mTxtTrustCertificate.setVisibility(View.GONE);
                mChkTrustCertificate.setChecked(false);
                mChkTrustCertificate.setVisibility(View.GONE);
            }
        });
        mEdtUserName = (EditText) findViewById(R.id.edt_user_name);
        mEdtPassword = (EditText) findViewById(R.id.edt_password);
        mTxtTrustCertificateDescription = (TextView) findViewById(R.id.txt_trust_certificate_description);
        mTxtTrustCertificate = (TextView) findViewById(R.id.txt_trust_certificate);
        mChkTrustCertificate = (CheckBox) findViewById(R.id.chk_trust_certificate);

        mTxtTrustCertificateDescription.setVisibility(View.GONE);
        mTxtTrustCertificate.setVisibility(View.GONE);
        mChkTrustCertificate.setVisibility(View.GONE);

        mBtnOk = (Button) findViewById(R.id.btn_ok);
        mBtnOk.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                onOkClick();
            }
        });
        findViewById(R.id.btn_cancel).setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                setResult(Activity.RESULT_CANCELED);
                finish();
            }
        });
    }