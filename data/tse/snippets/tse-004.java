/**
     * @param context
     *            Never <code>null</code>.
     * @throws NullPointerException
     *             If <tt>context</tt> is <code>null</code>.
     */
    protected StorageManager(final Context context) throws NullPointerException {
        if (context == null) {
            throw new NullPointerException("No Context given");
        }

        this.context = context;

        /*
         * 20101113/fiouzy:
         *
         * Here is where we define which providers are used, currently we only
         * allow the internal storage and the regular external storage.
         *
         * HTC Incredible storage and Samsung Galaxy S are omitted on purpose
         * (they're experimental and I don't have those devices to test).
         *
         *
         * !!! Make sure InternalStorageProvider is the first provider as it'll
         * be considered as the default provider !!!
         */
        final List<StorageProvider> allProviders = Arrays.asList(new InternalStorageProvider(),
                new ExternalStorageProvider());
        for (final StorageProvider provider : allProviders) {
            // check for provider compatibility
            if (provider.isSupported(context)) {
                // provider is compatible! proceeding

                provider.init(context);
                mProviders.put(provider.getId(), provider);
                mProviderLocks.put(provider, new SynchronizationAid());
            }
        }

    }