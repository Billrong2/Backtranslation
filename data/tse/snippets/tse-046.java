/**
   * Print out a short summary string for the dataset characteristics
   * 
   * @param attrType the attribute type (NUMERIC, NOMINAL, etc.)
   * @param classType the class type (NUMERIC, NOMINAL, etc.)
   */
  protected void printAttributeSummary(int attrType, int classType) {

    String str = "";

    switch (attrType) {
    case Attribute.NUMERIC:
      str = " numeric" + str;
      break;
    case Attribute.NOMINAL:
      str = " nominal" + str;
      break;
    case Attribute.STRING:
      str = " string" + str;
      break;
    case Attribute.DATE:
      str = " date" + str;
      break;
    case Attribute.RELATIONAL:
      str = " relational" + str;
      break;
    }
    str += " attribute(s))";

    switch (classType) {
    case Attribute.NUMERIC:
      str = " (numeric class," + str;
      break;
    case Attribute.NOMINAL:
      str = " (nominal class," + str;
      break;
    case Attribute.STRING:
      str = " (string class," + str;
      break;
    case Attribute.DATE:
      str = " (date class," + str;
      break;
    case Attribute.RELATIONAL:
      str = " (relational class," + str;
      break;
    }

    print(str);
  }