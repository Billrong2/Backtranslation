private void initWriterForSimpleCallbackTests() throws Exception {
		writer = createItemWriter();
		writer.setHeaderCallback(new StaxWriterCallback() {

			@Override
			public void write(XMLEventWriter writer) throws IOException {
				XMLEventFactory factory = XMLEventFactory.newInstance();
				try {
					writer.add(factory.createStartElement("ns", "http://www.springframework.org/test", "group"));
				}
				catch (XMLStreamException e) {
					throw new RuntimeException(e);
				}
			}

		});
		writer.setFooterCallback(new StaxWriterCallback() {

			@Override
			public void write(XMLEventWriter writer) throws IOException {
				XMLEventFactory factory = XMLEventFactory.newInstance();
				try {
					writer.add(factory.createEndElement("ns", "http://www.springframework.org/test", "group"));
				}
				catch (XMLStreamException e) {
					throw new RuntimeException(e);
				}

			}

		});
		writer.setRootTagName("{http://www.springframework.org/test}ns:testroot");
		writer.afterPropertiesSet();
	}