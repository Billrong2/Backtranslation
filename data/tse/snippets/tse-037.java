@Test
	public void testArtifactUniqueness() throws Exception {
		JobExecution jobExecution = runJob("jsrUniqueInstanceTests", new Properties(), 10000L);
		String exitStatus = jobExecution.getExitStatus();

		assertTrue("Exit status must contain listener3", exitStatus.contains("listener3"));
		exitStatus = exitStatus.replace("listener3", "");

		assertTrue("Exit status must contain listener2", exitStatus.contains("listener2"));
		exitStatus = exitStatus.replace("listener2", "");

		assertTrue("Exit status must contain listener1", exitStatus.contains("listener1"));
		exitStatus = exitStatus.replace("listener1", "");

		assertTrue("Exit status must contain listener0", exitStatus.contains("listener0"));
		exitStatus = exitStatus.replace("listener0", "");

		assertTrue("Exit status must contain listener7", exitStatus.contains("listener7"));
		exitStatus = exitStatus.replace("listener7", "");

		assertTrue("Exit status must contain listener6", exitStatus.contains("listener6"));
		exitStatus = exitStatus.replace("listener6", "");

		assertTrue("Exit status must contain listener5", exitStatus.contains("listener5"));
		exitStatus = exitStatus.replace("listener5", "");

		assertTrue("Exit status must contain listener4", exitStatus.contains("listener4"));
		exitStatus = exitStatus.replace("listener4", "");

		assertTrue("exitStatus must be empty", "".equals(exitStatus));
	}