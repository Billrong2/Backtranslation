@Test
    public void open_withXoauth2Extension_shouldThrowOnMultipleFailure() throws Exception {
        MockSmtpServer server = new MockSmtpServer();
        server.output("220 localhost Simple Mail Transfer Service Ready");
        server.expect("EHLO localhost");
        server.output("250-localhost Hello client.localhost");
        server.output("250 AUTH XOAUTH2");
        server.expect("AUTH XOAUTH2 dXNlcj11c2VyAWF1dGg9QmVhcmVyIG9sZFRva2VuAQE=");
        server.output("334 " + XOAuth2ChallengeParserTest.STATUS_400_RESPONSE);
        server.expect("");
        server.output("535-5.7.1 Username and Password not accepted. Learn more at");
        server.output("535 5.7.1 http://support.google.com/mail/bin/answer.py?answer=14257 hx9sm5317360pbc.68");
        server.expect("AUTH XOAUTH2 dXNlcj11c2VyAWF1dGg9QmVhcmVyIG5ld1Rva2VuAQE=");
        server.output("334 " + XOAuth2ChallengeParserTest.STATUS_400_RESPONSE);
        server.expect("");
        server.output("535-5.7.1 Username and Password not accepted. Learn more at");
        server.output("535 5.7.1 http://support.google.com/mail/bin/answer.py?answer=14257 hx9sm5317360pbc.68");
        SmtpTransport transport = startServerAndCreateSmtpTransport(server, AuthType.XOAUTH2, ConnectionSecurity.NONE);

        try {
            transport.open();
            fail("Exception expected");
        } catch (AuthenticationFailedException e) {
            assertEquals(
                "Negative SMTP reply: 535 5.7.1 http://support.google.com/mail/bin/answer.py?answer=14257 hx9sm5317360pbc.68",
                e.getMessage());
        }

        server.verifyConnectionStillOpen();
        server.verifyInteractionCompleted();
    }