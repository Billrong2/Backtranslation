@Test
	public void testFailureAndRecovery() throws Exception {
		final RetryTemplate retryTemplate = new RetryTemplate();
		retryTemplate.setRetryPolicy(new NeverRetryPolicy());
		container.setMessageListener(new MessageListener() {
			@Override
			public void onMessage(final Message msg) {
				try {
					RetryCallback<Message, Exception> callback = new RetryCallback<Message, Exception>() {
						@Override
						public Message doWithRetry(RetryContext context) throws Exception {
							try {
								processed.add(((TextMessage) msg).getText());
							}
							catch (JMSException e) {
								throw new IllegalStateException(e);
							}
							throw new RuntimeException("planned failure: " + msg);
						}
					};
					RecoveryCallback<Message> recoveryCallback = new RecoveryCallback<Message>() {
						@Override
						public Message recover(RetryContext context) {
							try {
								recovered.add(((TextMessage) msg).getText());
							}
							catch (JMSException e) {
								throw new IllegalStateException(e);
							}
							return msg;
						}
					};
					retryTemplate.execute(callback, recoveryCallback, new DefaultRetryState(msg.getJMSMessageID()));
				}
				catch (Exception e) {
					throw (RuntimeException) e;
				}
			}
		});
		container.initializeProxy();
		container.start();
		jmsTemplate.convertAndSend("queue", "foo");
		assertEquals("foo", processed.poll(5, TimeUnit.SECONDS));
		assertEquals("foo", recovered.poll(5, TimeUnit.SECONDS));
	}