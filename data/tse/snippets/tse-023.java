@Override
    public void configure(Context context) {
        
        this.createTableDdl = context.getString(FlumeConstants.CONFIG_TABLE_DDL);
        this.fullTableName = context.getString(FlumeConstants.CONFIG_TABLE);
        final String zookeeperQuorum = context.getString(FlumeConstants.CONFIG_ZK_QUORUM);
        final String ipJdbcURL = context.getString(FlumeConstants.CONFIG_JDBC_URL);
        this.batchSize = context.getInteger(FlumeConstants.CONFIG_BATCHSIZE, FlumeConstants.DEFAULT_BATCH_SIZE);
        final String columnNames = context.getString(CONFIG_COLUMN_NAMES);
        final String headersStr = context.getString(CONFIG_HEADER_NAMES);
        final String keyGeneratorType = context.getString(CONFIG_ROWKEY_TYPE_GENERATOR);
       
        Preconditions.checkNotNull(this.fullTableName,"Table name cannot be empty, please specify in the configuration file");
        if(!Strings.isNullOrEmpty(zookeeperQuorum)) {
            this.jdbcUrl = QueryUtil.getUrl(zookeeperQuorum);
        }
        if(!Strings.isNullOrEmpty(ipJdbcURL)) {
            this.jdbcUrl = ipJdbcURL;
        }
        Preconditions.checkNotNull(this.jdbcUrl,"Please specify either the zookeeper quorum or the jdbc url in the configuration file");
        Preconditions.checkNotNull(columnNames,"Column names cannot be empty, please specify in configuration file");
        for(String s : Splitter.on(DEFAULT_COLUMNS_DELIMITER).split(columnNames)) {
           colNames.add(s);
        }
        
         if(!Strings.isNullOrEmpty(headersStr)) {
            for(String s : Splitter.on(DEFAULT_COLUMNS_DELIMITER).split(headersStr)) {
                headers.add(s);
             }
        }
      
        if(!Strings.isNullOrEmpty(keyGeneratorType)) {
            try {
                keyGenerator =  DefaultKeyGenerator.valueOf(keyGeneratorType.toUpperCase());
                this.autoGenerateKey = true;
            } catch(IllegalArgumentException iae) {
                logger.error("An invalid key generator {} was specified in configuration file. Specify one of {}",keyGeneratorType,DefaultKeyGenerator.values());
                Throwables.propagate(iae);
            } 
        }
        
        logger.debug(" the jdbcUrl configured is {}",jdbcUrl);
        logger.debug(" columns configured are {}",colNames.toString());
        logger.debug(" headers configured are {}",headersStr);
        logger.debug(" the keyGenerator configured is {} ",keyGeneratorType);

        doConfigure(context);
        
    }