@Test
    public void testFetchResponse() throws Exception {
        ImapResponseParser parser = createParser("* 1 FETCH (" +
                "UID 23 " +
                "INTERNALDATE \"01-Jul-2015 12:34:56 +0200\" " +
                "RFC822.SIZE 3456 " +
                "BODY[HEADER.FIELDS (date subject from)] \"<headers>\" " +
                "FLAGS (\\Seen))\r\n");

        ImapResponse response = parser.readResponse();

        assertEquals(3, response.size());
        assertEquals("1", response.getString(0));
        assertEquals("FETCH", response.getString(1));
        assertEquals("UID", response.getList(2).getString(0));
        assertEquals(23, response.getList(2).getNumber(1));
        assertEquals("INTERNALDATE", response.getList(2).getString(2));
        assertEquals("01-Jul-2015 12:34:56 +0200", response.getList(2).getString(3));
        assertEquals("RFC822.SIZE", response.getList(2).getString(4));
        assertEquals(3456, response.getList(2).getNumber(5));
        assertEquals("BODY", response.getList(2).getString(6));
        assertEquals(2, response.getList(2).getList(7).size());
        assertEquals("HEADER.FIELDS", response.getList(2).getList(7).getString(0));
        assertEquals(3, response.getList(2).getList(7).getList(1).size());
        assertEquals("date", response.getList(2).getList(7).getList(1).getString(0));
        assertEquals("subject", response.getList(2).getList(7).getList(1).getString(1));
        assertEquals("from", response.getList(2).getList(7).getList(1).getString(2));
        assertEquals("<headers>", response.getList(2).getString(8));
        assertEquals("FLAGS", response.getList(2).getString(9));
        assertEquals(1, response.getList(2).getList(10).size());
        assertEquals("\\Seen", response.getList(2).getList(10).getString(0));
    }