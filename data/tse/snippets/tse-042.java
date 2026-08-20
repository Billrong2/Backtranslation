private ArrayList<ContentProviderOperation> getContentProviderOperationsForCreate(
      TransactionChange change, int offset, int parentOffset) {
    if (!change.isCreate()) throw new AssertionError();
    Long amount;
    if (change.amount() != null) {
      amount = change.amount();
    } else {
      amount = 0L;
    }
    Transaction t;
    long transferAccount;
    if (change.splitParts() != null) {
      t = new SplitTransaction(getAccount().getId(), amount);
    } else if (change.transferAccount() != null &&
        (transferAccount = extractTransferAccount(change.transferAccount(), change.label())) != -1) {
      t = new Transfer(getAccount().getId(), amount);
      t.transfer_account = transferAccount;
    } else {
      t = new Transaction(getAccount().getId(), amount);
      if (change.label() != null) {
        long catId = extractCatId(change.label());
        if (catId != -1) {
          t.setCatId(catId);
        }
      }
    }
    t.uuid = change.uuid();
    if (change.comment() != null) {
      t.comment = change.comment();
    }
    if (change.date() != null) {
      Long date = change.date();
      assert date != null;
      t.setDate(new Date(date * 1000));
    }

    if (change.payeeName() != null) {
      long id = Payee.extractPayeeId(change.payeeName(), payeeToId);
      if (id != -1) {
        t.payeeId = id;
      }
    }
    if (change.methodLabel() != null) {
      long id = extractMethodId(change.methodLabel());
      if (id != -1) {
        t.methodId = id;
      }
    }
    if (change.crStatus() != null) {
      t.crStatus = Transaction.CrStatus.valueOf(change.crStatus());
    }
    t.referenceNumber = change.referenceNumber();
    if (parentOffset == -1 && change.parentUuid() != null) {
      long parentId = Transaction.findByUuid(change.parentUuid());
      if (parentId == -1) {
        return new ArrayList<>(); //if we fail to link a split part to a parent, we need to ignore it
      }
      t.parentId = parentId;
    }
    if (change.pictureUri() != null) {
      t.setPictureUri(Uri.parse(change.pictureUri()));
    }
    return t.buildSaveOperations(offset, parentOffset, true);
  }