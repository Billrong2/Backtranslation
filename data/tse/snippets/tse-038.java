@Test
	public void testOperationWithDirectCloseCall() throws SQLException {
		Connection con = mock(Connection.class);
		DataSource ds = mock(DataSource.class);

		when(ds.getConnection()).thenReturn(con); // con1
		con.close();
		when(ds.getConnection()).thenReturn(con); // con2
		con.close();


		final ExtendedConnectionDataSourceProxy csds = new ExtendedConnectionDataSourceProxy(ds);

		Connection con1 = csds.getConnection();
		csds.startCloseSuppression(con1);
		Connection con1_1 = csds.getConnection();
		assertSame("should be same connection", con1_1, con1);
		con1_1.close(); // no mock call for this - should be suppressed
		Connection con1_2 = csds.getConnection();
		assertSame("should be same connection", con1_2, con1);
		Connection con2 = csds.getConnection();
		assertNotSame("shouldn't be same connection", con2, con1);
		csds.stopCloseSuppression(con1);
		assertTrue("should be able to close connection", csds.shouldClose(con1));
		con1_1 = null;
		con1_2 = null;
		con1.close();
		assertTrue("should be able to close connection", csds.shouldClose(con2));
		con2.close();


	}