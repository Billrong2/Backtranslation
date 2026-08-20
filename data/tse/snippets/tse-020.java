@Test public void testToStringStartStop2() throws Exception {
		LexerGrammar g = new LexerGrammar(
											 "lexer grammar T;\n"+
											 "ID : 'a'..'z'+;\n" +
											 "INT : '0'..'9'+;\n" +
											 "SEMI : ';';\n" +
											 "ASSIGN : '=';\n" +
											 "PLUS : '+';\n" +
											 "MULT : '*';\n" +
											 "WS : ' '+;\n");
		// Tokens: 012345678901234567
		// Input:  x = 3 * 0 + 2 * 0;
		String input = "x = 3 * 0 + 2 * 0;";
		LexerInterpreter lexEngine = g.createLexerInterpreter(new ANTLRInputStream(input));
		CommonTokenStream stream = new CommonTokenStream(lexEngine);
		stream.fill();
		TokenStreamRewriter tokens = new TokenStreamRewriter(stream);

		String result = tokens.getTokenStream().getText();
		String expecting = "x = 3 * 0 + 2 * 0;";
		assertEquals(expecting, result);

		tokens.replace(4, 8, "0");
		stream.fill();
// replace 3 * 0 with 0
		result = tokens.getText();
		expecting = "x = 0 + 2 * 0;";
		assertEquals(expecting, result);

		result = tokens.getText(Interval.of(0, 17));
		expecting = "x = 0 + 2 * 0;";
		assertEquals(expecting, result);

		result = tokens.getText(Interval.of(4, 8));
		expecting = "0";
		assertEquals(expecting, result);

		result = tokens.getText(Interval.of(0, 8));
		expecting = "x = 0";
		assertEquals(expecting, result);

		result = tokens.getText(Interval.of(12, 16));
		expecting = "2 * 0";
		assertEquals(expecting, result);

		tokens.insertAfter(17, "// comment");
		result = tokens.getText(Interval.of(12, 18));
		expecting = "2 * 0;// comment";
		assertEquals(expecting, result);

		result = tokens.getText(Interval.of(0, 8));
		stream.fill();
// try again after insert at end
		expecting = "x = 0";
		assertEquals(expecting, result);
	}