/**
   * Handles the various button clicking type activities.
   * 
   * @param e a value of type 'ActionEvent'
   */
  @Override
  public void actionPerformed(ActionEvent e) {

    if (e.getSource() == m_ConfigureBut) {
      selectProperty();
    } else if (e.getSource() == m_StatusBox) {
      // notify any listeners
      for (int i = 0; i < m_Listeners.size(); i++) {
        ActionListener temp = (m_Listeners.get(i));
        temp.actionPerformed(new ActionEvent(this,
          ActionEvent.ACTION_PERFORMED, "Editor status change"));
      }

      // Toggles whether the custom property is used
      if (m_StatusBox.getSelectedIndex() == 0) {
        m_Exp.setUsePropertyIterator(false);
        m_ConfigureBut.setEnabled(false);
        m_ArrayEditor.getCustomEditor().setEnabled(false);
        m_ArrayEditor.setValue(null);
        validate();
      } else {
        if (m_Exp.getPropertyArray() == null) {
          selectProperty();
        }
        if (m_Exp.getPropertyArray() == null) {
          m_StatusBox.setSelectedIndex(0);
        } else {
          m_Exp.setUsePropertyIterator(true);
          m_ConfigureBut.setEnabled(true);
          m_ArrayEditor.getCustomEditor().setEnabled(true);
        }
        validate();
      }
    }
  }