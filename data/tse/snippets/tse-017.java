public void process() throws Exception {
//		System.out.println("exec "+grammarName+"."+startRuleName);
		String lexerName = grammarName+"Lexer";
		ClassLoader cl = Thread.currentThread().getContextClassLoader();
		Class<? extends Lexer> lexerClass = null;
		try {
			lexerClass = cl.loadClass(lexerName).asSubclass(Lexer.class);
		}
		catch (java.lang.ClassNotFoundException cnfe) {
			// might be pure lexer grammar; no Lexer suffix then
			lexerName = grammarName;
			try {
				lexerClass = cl.loadClass(lexerName).asSubclass(Lexer.class);
			}
			catch (ClassNotFoundException cnfe2) {
				System.err.println("Can't load "+lexerName+" as lexer or parser");
				return;
			}
		}

		Constructor<? extends Lexer> lexerCtor = lexerClass.getConstructor(CharStream.class);
		Lexer lexer = lexerCtor.newInstance((CharStream)null);

		Class<? extends Parser> parserClass = null;
		Parser parser = null;
		if ( !startRuleName.equals(LEXER_START_RULE_NAME) ) {
			String parserName = grammarName+"Parser";
			parserClass = cl.loadClass(parserName).asSubclass(Parser.class);
			Constructor<? extends Parser> parserCtor = parserClass.getConstructor(TokenStream.class);
			parser = parserCtor.newInstance((TokenStream)null);
		}

		Charset charset = ( encoding == null ? Charset.defaultCharset () : Charset.forName(encoding) );
		if ( inputFiles.size()==0 ) {
			CharStream charStream;
			if ( charset.equals(StandardCharsets.UTF_8)) {
				charStream = CharStreams.createWithUTF8Stream(System.in);
			} else {
				try ( InputStreamReader r = new InputStreamReader(System.in, charset) ) {
					charStream = new ANTLRInputStream(r);
			}
			}
			process(lexer, parserClass, parser, charStream);
			return;
		}
		for (String inputFile : inputFiles) {
			CharStream charStream;
			if ( charset.equals(StandardCharsets.UTF_8) ) {
				charStream = CharStreams.createWithUTF8(Paths.get(inputFile));
			} else {
				try ( InputStreamReader r = new InputStreamReader(System.in, charset) ) {
					charStream = new ANTLRInputStream(r);
			}
			}
			if ( inputFiles.size()>1 ) {
				System.err.println(inputFile);
			}
			process(lexer, parserClass, parser, charStream);
		}
	}