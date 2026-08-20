/**
     * Executes the commands from the given reader in this shell.<p>
     *
     * <ul>
     * <li>Commands in the must be separated with a line break '\n'.
     * <li>Only one command per line is allowed.
     * <li>String parameters must be quoted like this: <code>'string value'</code>.
     * </ul>
     *
     * @param reader the reader from which the commands are read
     */
    public void execute(Reader reader) {

        try {
            LineNumberReader lnr = new LineNumberReader(reader);
            while (!m_exitCalled) {
                String line = lnr.readLine();
                if (line != null) {
                    if (m_interactive || m_echo) {
                        // print the prompt in front of the commands to process only when 'interactive'
                        printPrompt();
                    }
                } else {
                    // if null the file has been read to the end
                    try {
                        Thread.sleep(500);
                    } catch (Throwable t) {
                        // noop
                    }
                    // end the while loop
                    break;
                }
                if (line.trim().startsWith("#")) {
                    m_out.println(line);
                    continue;
                }
                StringReader lineReader = new StringReader(line);
                StreamTokenizer st = new StreamTokenizer(lineReader);
                st.eolIsSignificant(true);
                st.wordChars('*', '*');
                // put all tokens into a List
                List<String> parameters = new ArrayList<String>();
                while (st.nextToken() != StreamTokenizer.TT_EOF) {
                    if (st.ttype == StreamTokenizer.TT_NUMBER) {
                        parameters.add(Integer.toString(new Double(st.nval).intValue()));
                    } else {
                        parameters.add(st.sval);
                    }
                }
                lineReader.close();

                if (parameters.size() == 0) {
                    // empty line, just need to check if echo is on
                    if (m_echo) {
                        m_out.println();
                    }
                    continue;
                }

                // extract command and arguments
                String command = parameters.get(0);
                List<String> arguments = parameters.subList(1, parameters.size());

                // execute the command with the given arguments
                executeCommand(command, arguments);
            }
        } catch (Throwable t) {
            t.printStackTrace(m_err);
        }
    }