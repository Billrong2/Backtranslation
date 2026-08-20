/**
     * Get an instance of a remote mail store.
     */
    public static synchronized Store getInstance(Context context, StoreConfig storeConfig) throws MessagingException {
        String uri = storeConfig.getStoreUri();

        if (uri.startsWith("local")) {
            throw new RuntimeException("Asked to get non-local Store object but given LocalStore URI");
        }

        Store store = sStores.get(uri);
        if (store == null) {
            if (uri.startsWith("imap")) {
                OAuth2TokenProvider oAuth2TokenProvider = null;
                store = new ImapStore(
                        storeConfig,
                        new DefaultTrustedSocketFactory(context),
                        (ConnectivityManager) context.getSystemService(Context.CONNECTIVITY_SERVICE),
                        oAuth2TokenProvider);
            } else if (uri.startsWith("pop3")) {
                store = new Pop3Store(storeConfig, new DefaultTrustedSocketFactory(context));
            } else if (uri.startsWith("webdav")) {
                store = new WebDavStore(storeConfig, new WebDavHttpClient.WebDavHttpClientFactory());
            }

            if (store != null) {
                sStores.put(uri, store);
            }
        }

        if (store == null) {
            throw new MessagingException("Unable to locate an applicable Store for " + uri);
        }

        return store;
    }