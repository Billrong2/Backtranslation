@Override
    public Status process() throws EventDeliveryException {
        
        Status status = Status.READY;
        Channel channel = getChannel();
        Transaction transaction = null;
        List<Event>  events = Lists.newArrayListWithExpectedSize(this.batchSize); 
        long startTime = System.nanoTime();
        try {
            transaction = channel.getTransaction();
            transaction.begin();
            
            for(long i = 0; i < this.batchSize; i++) {
                Event event = channel.take();
                if(event == null){
                  status = Status.BACKOFF;
                  if (i == 0) {
                    sinkCounter.incrementBatchEmptyCount();
                  } else {
                    sinkCounter.incrementBatchUnderflowCount();
                  }
                  break;
                } else {
                  events.add(event);
                }
            }
            if (!events.isEmpty()) {
               if (events.size() == this.batchSize) {
                    sinkCounter.incrementBatchCompleteCount();
                }
                else {
                    sinkCounter.incrementBatchUnderflowCount();
                    status = Status.BACKOFF;
                }
                // save to Hbase
                serializer.upsertEvents(events);
                sinkCounter.addToEventDrainSuccessCount(events.size());
            }
            else {
                logger.debug("no events to process ");
                sinkCounter.incrementBatchEmptyCount();
                status = Status.BACKOFF;
            }
            transaction.commit();
        } catch (ChannelException e) {
            transaction.rollback();
            status = Status.BACKOFF;
            sinkCounter.incrementConnectionFailedCount();
        }
        catch (SQLException e) {
            sinkCounter.incrementConnectionFailedCount();
            transaction.rollback();
            logger.error("exception while persisting to Hbase ", e);
            throw new EventDeliveryException("Failed to persist message to Hbase", e);
        }
        catch (Throwable e) {
            transaction.rollback();
            logger.error("exception while processing in Phoenix Sink", e);
            throw new EventDeliveryException("Failed to persist message", e);
        }
        finally {
            logger.info(String.format("Time taken to process [%s] events was [%s] seconds",
                    events.size(),
                    TimeUnit.SECONDS.convert(System.nanoTime() - startTime, TimeUnit.NANOSECONDS)));
            if( transaction != null ) {
                transaction.close();
            }
        }
        return status;
   }