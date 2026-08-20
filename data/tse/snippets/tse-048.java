private void addButtons() {
    JButton okBut = new JButton("OK");
    JButton cancelBut = new JButton("Cancel");

    JPanel butHolder = new JPanel();
    butHolder.setLayout(new GridLayout(1, 2));
    butHolder.add(okBut);
    butHolder.add(cancelBut);
    add(butHolder, BorderLayout.SOUTH);

    okBut.addActionListener(new ActionListener() {
      @Override
      public void actionPerformed(ActionEvent e) {
        if (m_modifyListener != null) {
          m_modifyListener.setModifiedStatus(
              ClassifierPerformanceEvaluatorCustomizer.this, true);
        }

        if (m_evaluationMetrics.size() > 0) {
          StringBuilder b = new StringBuilder();
          for (String s : m_evaluationMetrics) {
            b.append(s).append(",");
          }
          String newList = b.substring(0, b.length() - 1);
          m_cpe.setEvaluationMetricsToOutput(newList);
        }
        if (m_parent != null) {
          m_parent.dispose();
        }
      }
    });

    cancelBut.addActionListener(new ActionListener() {
      @Override
      public void actionPerformed(ActionEvent e) {

        customizerClosing();
        if (m_parent != null) {
          m_parent.dispose();
        }
      }
    });
  }