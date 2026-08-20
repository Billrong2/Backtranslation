/**
   * Describe <code>performRequest</code> method here.
   * 
   * @param request a <code>String</code> value
   * @exception IllegalArgumentException if an error occurs
   */
  @Override
  public void performRequest(String request) {
    if (request.compareTo("Show chart") == 0) {
      try {
        // popup visualize panel
        if (!m_framePoppedUp) {
          m_framePoppedUp = true;

          final javax.swing.JFrame jf = new javax.swing.JFrame(
            "Model Performance Chart");
          jf.setSize(800, 600);
          jf.getContentPane().setLayout(new BorderLayout());
          jf.getContentPane().add(m_visPanel, BorderLayout.CENTER);
          jf.addWindowListener(new java.awt.event.WindowAdapter() {
            @Override
            public void windowClosing(java.awt.event.WindowEvent e) {
              jf.dispose();
              m_framePoppedUp = false;
            }
          });
          jf.setVisible(true);
          m_popupFrame = jf;
        } else {
          m_popupFrame.toFront();
        }
      } catch (Exception ex) {
        ex.printStackTrace();
        m_framePoppedUp = false;
      }
    } else if (request.equals("Clear all plots")) {
      m_visPanel.removeAllPlots();
      m_visPanel.validate();
      m_visPanel.repaint();
      m_visPanel = null;
      m_masterPlot = null;
      m_offscreenPlotData = null;
    } else {
      throw new IllegalArgumentException(request
        + " not supported (Model Performance Chart)");
    }
  }