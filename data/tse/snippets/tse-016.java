/**
     * Performs dependency analysis for the given grammar files.
     *
     * @param   grammarFiles        the grammar files.
     * @param   importGrammarFiles  the import grammar files.
     * @param   tool                the tool to use.
     *
     * @return  self-reference.
     */
    public GrammarDependencies analyze(Set<File> grammarFiles,
        Set<File> importGrammarFiles, Tool tool) throws IOException {
        log.debug("Analysing grammar dependencies " + sourceDirectory);

        // for dependency analysis we require all grammars
        Collection<File> grammarsAndTokens = new HashSet<File>();
        grammarsAndTokens.addAll(importGrammarFiles);
        grammarsAndTokens.addAll(grammarFiles);

        for (File grammarFile : grammarsAndTokens) {
            // .tokens files must not be parsed, they can just be referenced
            if (!grammarFile.getName().endsWith(".tokens"))
                analyse(grammarFile, grammarsAndTokens, tool);
        }

        for (File grammarFile : grammarFiles) {
            Collection<String> usages = findUsages(getRelativePath(grammarFile));

            if (!usages.isEmpty()) {
                grammars.put(grammarFile,
                    new AbstractMap.SimpleImmutableEntry<byte[], Collection<String>>(
                        MojoUtils.checksum(grammarFile), usages));

                log.debug("  " + getRelativePath(grammarFile) + " used by " + usages);
            }
        }

        for (File grammarFile : importGrammarFiles) {
            // imported files are not allowed to be qualified
            Collection<String> usages = findUsages(grammarFile.getName());

            if (!usages.isEmpty()) {
                grammars.put(grammarFile,
                    new AbstractMap.SimpleImmutableEntry<byte[], Collection<String>>(
                        MojoUtils.checksum(grammarFile), usages));

                log.debug("  " + grammarFile.getName() + " imported by " + usages);
            }
        }

        return this;
    }